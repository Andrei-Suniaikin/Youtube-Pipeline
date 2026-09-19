#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Квота YouTube Data API: общий кошелёк, суточный учёт и «403 — это подождать».

Какой дефект ловится. Ключ YouTube Data API один на всё — поздняя проверка выхода и
ночной обход конкурентов ВВ2 тратят одни и те же единицы. До этой работы 403 по квоте
был для нас обычной ошибкой запроса: проход записывал её результатом проверки ролика,
шёл дальше и получал тот же 403 на каждом следующем выпуске, а владелец видел ошибку
там, где ролик ни при чём. Ровно это случилось 18.09 в 01:00 — прогон упал на первом же
403, остаток остался необработанным до утра, догоняли руками в 10:09.

Чем грозило: настоящая проверка откладывалась на RECHECK_SEC (полчаса) за каждый
выброшенный впустую ответ, в журнале выпуска копилась ложная причина, а понять, что
единицы выел другой потребитель того же ключа, было неоткуда.

Сети нет: транспорт подменяется, как в tests/test_verify_public.py.

    .venv/bin/python tests/test_yt_quota.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_calls as pc                                            # noqa: E402
import pub_verify as V                                            # noqa: E402

HOUR = 3600.0
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def ts(text: str) -> float:
    """«2026-09-18 08:30 UTC» → момент времени. Так проверки читаются глазами."""
    return dt.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(
        tzinfo=dt.timezone.utc).timestamp()


NOW = ts("2026-09-18 12:00")          # 18.09, полдень UTC = 05:00 в Калифорнии


def fresh_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
      CREATE TABLE pub_releases(id INTEGER PRIMARY KEY, release_id TEXT, tenant_id TEXT,
                                plan_item_id INT, slot_at REAL, phase TEXT, status TEXT,
                                video_id TEXT, blocked_reason TEXT, updated REAL);
      CREATE TABLE pub_release_events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                                release_id TEXT, seq INTEGER, phase TEXT, video_id TEXT,
                                payload TEXT, created REAL,
                                repeats INTEGER NOT NULL DEFAULT 1, last_seen REAL,
                                UNIQUE(release_id,seq));
      CREATE TABLE pub_release_log(id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT,
                                ts REAL, level TEXT, step TEXT, message TEXT,
                                repeats INTEGER NOT NULL DEFAULT 1, last_ts REAL);
    """)
    pc.ensure_calls_schema(c)
    return c


def with_releases(n=3, slot=None):
    """База с n выпусками, у которых слот давно прошёл — их пора проверять."""
    c = fresh_db()
    slot = NOW - 3 * HOUR if slot is None else slot
    for i in range(1, n + 1):
        c.execute("INSERT INTO pub_releases VALUES(?,?,?,?,?,?,?,?,NULL,?)",
                  (i, f"r{i}", "dev", i, slot, "scheduled", "done", f"vid{i}", NOW))
    c.commit()
    return c


def http_error(code: int, reason: str, message: str = "quota exceeded",
               domain: str = "youtube.quota"):
    """Ответ Google в том виде, в каком его отдаёт urllib: HTTPError с телом json.

    Домен ошибки у Google так и называется — `youtube.quota`, причём и у отказов,
    которые к квоте отношения не имеют. Поэтому фикстура оставляет его по умолчанию:
    разбор обязан смотреть на `reason`, а не на слово «quota» где-нибудь в теле.
    """
    import io
    body = ('{"error":{"code":%d,"message":"%s","errors":[{"domain":"%s",'
            '"reason":"%s"}]}}' % (code, message, domain, reason)).encode("utf-8")

    def fetch(url):
        raise urllib.error.HTTPError(url, code, "Forbidden", {},
                                     io.BytesIO(body))
    return fetch


def counting(inner):
    """Обёртка над транспортом, считающая, сколько раз мы реально пошли в сеть."""
    box = {"n": 0}

    def fetch(url):
        box["n"] += 1
        return inner(url)
    fetch.hits = box                                              # type: ignore[attr-defined]
    return fetch


def collect():
    """Консольный колбэк прохода, запоминающий сказанное. Это то, что видит оператор."""
    lines = []

    def log(*a):
        lines.append(" ".join(str(x) for x in a))
    log.lines = lines                                             # type: ignore[attr-defined]
    return log


def broken_json(text: str) -> bool:
    """Правда ли, что тело НЕ разбирается как json (обрыв, не json) — для запасного пути."""
    try:
        json.loads(text)
    except ValueError:
        return True
    return False


def ok_answer(privacy="public"):
    return lambda url: {"items": [{"status": {"privacyStatus": privacy,
                                              "uploadStatus": "processed"},
                                   "snippet": {"publishedAt": "2026-09-18T10:00:00Z"}}]}


# ─────────────────────── сутки квоты: тихоокеанская полночь ───────────────────────

def test_pacific_day():
    print("\nСутки квоты считаются по тихоокеанской полуночи, а не по UTC")
    # Зимой Калифорния — UTC−8: 07:59 UTC ещё вчерашние сутки квоты, 08:01 — уже новые.
    check("зима, 07:59 UTC — ещё 14 января", pc.quota_day(ts("2027-01-15 07:59")),
          "2027-01-14")
    check("зима, 08:01 UTC — уже 15 января", pc.quota_day(ts("2027-01-15 08:01")),
          "2027-01-15")
    # Летом там же UTC−7, и граница суток сдвигается на час. Фиксированный сдвиг дважды
    # в год ошибался бы ровно здесь: мы бы либо ждали лишний час, либо тратили до сброса.
    check("лето, 06:59 UTC — ещё 14 июля", pc.quota_day(ts("2027-07-15 06:59")),
          "2027-07-14")
    check("лето, 07:01 UTC — уже 15 июля", pc.quota_day(ts("2027-07-15 07:01")),
          "2027-07-15")
    # Сброс — ближайшая тихоокеанская полночь, а не «сейчас плюс сутки».
    check("сброс зимой ровно в 08:00 UTC следующего дня",
          pc.quota_reset_at(ts("2027-01-15 12:00")), ts("2027-01-16 08:00"))
    check("сброс летом ровно в 07:00 UTC следующего дня",
          pc.quota_reset_at(ts("2027-07-15 12:00")), ts("2027-07-16 07:00"))


def test_usage_accounting():
    print("\nУчёт: кто сколько потратил за сутки")
    c = fresh_db()
    pc.quota_spend(c, "verify", 1, now=NOW)
    pc.quota_spend(c, "verify", 1, now=NOW + 60)
    pc.quota_spend(c, "ww2", pc.QUOTA_COSTS["search.list"], now=NOW + 120)
    check("всего за сутки", pc.quota_used(c, now=NOW), 102)
    check("самый жадный — первым",
          [(r["consumer"], r["units"], r["calls"]) for r in pc.quota_usage(c, now=NOW)],
          [("ww2", 100, 1), ("verify", 2, 2)])
    # Сутки кончились — счётчик начинается заново, старые строки остаются историей.
    после = ts("2026-09-19 12:00")
    check("после тихоокеанской полуночи счётчик чистый", pc.quota_used(c, now=после), 0)
    check("вчерашний расход не потерян",
          pc.quota_used(c, now=NOW), 102)


def test_budget_setting():
    print("\nБюджет: умолчание 10000 единиц и настройка")
    check("умолчание — суточная квота Google по умолчанию", pc.YT_DAILY_UNITS, 10000)
    c = fresh_db()
    check("пока не потратили — можно", pc.quota_allow(c, "verify", 1, now=NOW)["ok"], True)
    pc.quota_spend(c, "verify", 10, now=NOW)
    # Бюджет подменяем аргументом: так проверяется правило, а не значение переменной.
    stop = pc.quota_allow(c, "verify", 1, now=NOW, budget=10)
    check("свой бюджет исчерпан — дальше нельзя", stop["ok"], False)
    check("и сказано словами, а не кодом", "бюджет" in stop["why"], True)
    check("сколько осталось — видно", stop["left"], 0)
    check("под бюджетом всё ещё можно",
          pc.quota_allow(c, "verify", 1, now=NOW, budget=11)["ok"], True)


def test_wait_record():
    print("\nОжидание сброса: заводится один раз и само протухает")
    c = fresh_db()
    first = pc.quota_block(c, "403 quotaExceeded", now=NOW)
    check("ожидание началось сейчас", first["fresh"], True)
    check("ждём до тихоокеанской полуночи", first["until"], pc.quota_reset_at(NOW))
    again = pc.quota_block(c, "403 quotaExceeded", now=NOW + 60)
    check("второй 403 новое ожидание НЕ заводит", again["fresh"], False)
    check("и срок не переставляет", again["until"], first["until"])
    check("пока ждём — тратить нельзя",
          pc.quota_allow(c, "verify", 1, now=NOW + 60)["ok"], False)
    check("после сброса ожидание протухло само",
          pc.quota_wait(c, now=first["until"] + 1), {})
    check("и тратить снова можно",
          pc.quota_allow(c, "verify", 1, now=first["until"] + 1)["ok"], True)


def test_reason_words():
    print("\nПричина ожидания — словами, с уликой про общий ключ")
    c = fresh_db()
    pc.quota_spend(c, "verify", 47, now=NOW)          # наш расход — капля
    rec = pc.quota_block(c, "403", now=NOW)
    why = pc.quota_why(rec)
    check("сказано, что ждём сброса", "ждём сброса" in why, True)
    check("сказано, когда именно", "тихоокеанск" in why, True)
    # Две числа — улика, но не приговор: бюджет 10000 это НАША настройка, а не то, что
    # Google выдал проекту. Назначать виноватым «другого потребителя» как факт нельзя —
    # при заниженной реальной квоте виноваты мы сами, и чинить надо VS_YT_QUOTA_UNITS.
    check("названа возможность про общий ключ",
          "либо единицы выел другой потребитель того же ключа" in why, True)
    check("и вторая — что бюджет не совпадает с реальной квотой",
          "либо бюджет VS_YT_QUOTA_UNITS не совпадает с реальной квотой проекта" in why,
          True)
    check("виноватый не назначен как факт", "остальное потратил" in why, False)
    check("наш расход назван числом", "47 из 10000" in why, True)
    # «Слишком часто» — это не суточная квота, и ждать до полуночи из-за неё не за что.
    c2 = fresh_db()
    rate = pc.quota_block(c2, "429", kind="rate", now=NOW)
    check("частота ждёт минуты, а не сутки", rate["until"], NOW + pc.YT_RATE_WAIT_SEC)
    check("и говорит про частоту", "частоту" in pc.quota_why(rate), True)


# ───────────────────── 403 по квоте в поздней проверке ─────────────────────

def test_quota_is_not_a_video_error():
    print("\n403 по квоте — это «повторить позже», а не ошибка ролика")
    c = with_releases(3)
    fetch = counting(http_error(403, "quotaExceeded"))
    out = V.run(c, "dev", NOW, fetch=fetch, key="k")
    check("в сеть сходили ОДИН раз, а не за каждым выпуском", fetch.hits["n"], 1)
    check("все три выпуска ждут сброса", out["quota_wait"], 3)
    check("ни один не засчитан проверенным", out["checked"], 0)
    check("и ни один не объявлен непонятной ошибкой", out["unknown"], 0)
    check("фазы не тронуты",
          [r[0] for r in c.execute("SELECT phase FROM pub_releases ORDER BY id")],
          ["scheduled"] * 3)
    check("выпуски не помечены сломанными",
          [r[0] for r in c.execute("SELECT blocked_reason FROM pub_releases")],
          [None] * 3)
    check("ответ НЕ записан результатом проверки ролика",
          c.execute("SELECT COUNT(*) FROM pub_release_events").fetchone()[0], 0)
    check("причина прохода названа словами", "квота" in out.get("why", ""), True)
    check("в отчёте видно состояние кошелька", out["quota"]["waiting"], True)


def test_reason_logged_once():
    print("\nПричина ожидания в журнале — один раз, а не на каждом проходе")
    c = with_releases(2)
    fetch = counting(http_error(403, "quotaExceeded"))
    V.run(c, "dev", NOW, fetch=fetch, key="k")
    V.run(c, "dev", NOW + 60, fetch=fetch, key="k")
    V.run(c, "dev", NOW + 120, fetch=fetch, key="k")
    check("следующие проходы в сеть не ходят вовсе", fetch.hits["n"], 1)
    rows = c.execute("SELECT message, repeats FROM pub_release_log").fetchall()
    check("строка про ожидание ровно одна", len(rows), 1)
    check("и это не повтор той же строки", rows[0][1], 1)
    check("она объясняет ожидание", "квота" in rows[0][0], True)


def test_reset_returns_to_work():
    print("\nПосле тихоокеанской полуночи проход возвращается к работе сам")
    c = with_releases(1)
    V.run(c, "dev", NOW, fetch=http_error(403, "quotaExceeded"), key="k")
    после = pc.quota_reset_at(NOW) + 60
    fetch = counting(ok_answer("public"))
    out = V.run(c, "dev", после, fetch=fetch, key="k")
    check("спросили снова", fetch.hits["n"], 1)
    check("и подтвердили выход", out["published"], 1)
    check("фаза повышена",
          c.execute("SELECT phase FROM pub_releases WHERE id=1").fetchone()[0],
          "published")


def test_spend_is_counted():
    print("\nРасход считается по факту ответа, а не по намерению")
    c = with_releases(2)
    V.run(c, "dev", NOW, fetch=ok_answer("public"), key="k")
    check("два videos.list = две единицы", pc.quota_used(c, now=NOW), 2)
    check("и записаны на поздней проверке",
          pc.quota_usage(c, now=NOW), [{"consumer": "verify", "units": 2, "calls": 2}])
    # Сеть не ответила — единиц это не стоило: Google списывает за состоявшийся запрос.
    c2 = with_releases(1)

    def boom(url):
        raise OSError("сеть недоступна")

    out = V.run(c2, "dev", NOW, fetch=boom, key="k")
    check("обрыв связи не списывает единицы", pc.quota_used(c2, now=NOW), 0)
    check("и по-прежнему ничего не решает", out["unknown"], 1)


def test_own_budget_stops_before_403():
    print("\nСвой счётчик упёрся в бюджет — в сеть не идём вовсе")
    c = with_releases(2)
    pc.quota_spend(c, "ww2", pc.YT_DAILY_UNITS, now=NOW)       # ночной обход всё выел
    fetch = counting(ok_answer("public"))
    out = V.run(c, "dev", NOW, fetch=fetch, key="k")
    check("ни одного запроса", fetch.hits["n"], 0)
    check("оба выпуска ждут", out["quota_wait"], 2)
    check("сказано про бюджет", "бюджет" in out.get("why", ""), True)
    check("фазы целы",
          [r[0] for r in c.execute("SELECT phase FROM pub_releases ORDER BY id")],
          ["scheduled"] * 2)


def test_other_403_is_not_quota():
    print("\n403 про сам ключ квотой не считается — ждать до полуночи тут нечего")
    c = with_releases(1)
    out = V.run(c, "dev", NOW,
                fetch=http_error(403, "accessNotConfigured",
                                 "YouTube Data API has not been used in project"),
                key="k")
    check("это не ожидание квоты", out["quota_wait"], 0)
    check("а честное «ответа нет»", out["unknown"], 1)
    check("ожидание сброса не заведено", pc.quota_wait(c, now=NOW), {})
    check("выпуск всё равно не помечен сломанным",
          c.execute("SELECT blocked_reason FROM pub_releases").fetchone()[0], None)
    check("запрос до Google дошёл — единица списана", pc.quota_used(c, now=NOW), 1)


def test_reason_is_said_out_loud_every_pass():
    print("\nПричина ожидания говорится вслух на КАЖДОМ проходе, а не только на первом")
    # Симптом ночи 18.09 — «ничего не произошло, а почему, неизвестно». Со второго
    # прохода проход разворачивается на своей бюджетной ветке, и раньше она молчала:
    # оператор, запустивший проверку руками во время ожидания, видел пустой вывод.
    c = with_releases(2)
    fetch = counting(http_error(403, "quotaExceeded"))
    first = collect()
    V.run(c, "dev", NOW, fetch=fetch, key="k", log=first)
    check("первый проход объяснил, почему встал",
          any("квота" in ln for ln in first.lines), True)
    second = collect()
    out = V.run(c, "dev", NOW + 60, fetch=fetch, key="k", log=second)
    check("второй проход в сеть не ходил", fetch.hits["n"], 1)
    check("и всё равно объяснил, почему стоит",
          any("квота" in ln for ln in second.lines), True)
    check("та же причина есть и в отчёте", "квота" in out["why"], True)
    # Собственный счётчик — отдельный путь в ту же остановку, и он тоже обязан говорить.
    c2 = with_releases(1)
    pc.quota_spend(c2, "ww2", pc.YT_DAILY_UNITS, now=NOW)
    third = collect()
    V.run(c2, "dev", NOW, fetch=counting(ok_answer()), key="k", log=third)
    check("своя бюджетная ветка тоже объясняет себя",
          any("бюджет" in ln for ln in third.lines), True)


def test_report_always_has_quota_and_why():
    print("\nВ отчёте прохода кошелёк и причина есть ВСЕГДА, даже на ранних возвратах")
    # Самый частый исход — «проверять нечего». Раньше оба поля появлялись только в конце
    # run(), и читатель отчёта (экран, dev/verify_public.py) падал бы на KeyError именно
    # тогда, когда всё в порядке.
    c = fresh_db()
    out = V.run(c, "dev", NOW, key="k")
    check("нечего проверять — кошелёк на месте", out["quota"]["day"], pc.quota_day(NOW))
    check("и причина есть, пусть и пустая", out["why"], "")
    c2 = with_releases(1)
    out2 = V.run(c2, "dev", NOW, key="")
    check("нет ключа — кошелёк всё равно в отчёте", out2["quota"]["waiting"], False)
    check("а причина названа словами", "ключа" in out2["why"], True)
    check("кошелёк отчёта читается как целое", sorted(out2["quota"])[:3],
          ["budget", "by_consumer", "day"])


def test_unregistered_is_not_daily_quota():
    print("\n«Запрос ушёл без ключа» суточной квотой не считается")
    # dailyLimitExceededUnreg означает «запрос ушёл неавторизованным» — это поломка
    # настройки, которую чинит человек. Внутри имени лежит имя настоящей причины, и
    # подстрочный разбор объявлял его квотой: система молча встала бы до полуночи.
    body = ('{"error":{"code":403,"errors":[{"domain":"usageLimits",'
            '"reason":"dailyLimitExceededUnreg","message":"Daily Limit for '
            'Unauthenticated Use Exceeded"}]}}')
    check("разбор по json — не квота", V.quota_kind(403, body), "")
    # То же тело, оборванное на полуслове: json не разбирается, работает запасной путь.
    broken = body.split(',"message"')[0]
    check("тело действительно не разбирается", broken_json(broken), True)
    check("имя причины в обрывке целое", "dailyLimitExceededUnreg" in broken, True)
    check("запасной разбор — тоже не квота", V.quota_kind(403, broken), "")
    # Настоящая суточная причина обоими путями остаётся квотой: граница слова не должна
    # сломать распознавание того, ради чего разбор и написан.
    real = '{"error":{"code":403,"errors":[{"reason":"dailyLimitExceeded"}]}}'
    check("настоящая суточная причина — квота", V.quota_kind(403, real), "daily")
    real_broken = real.split('"}]}}')[0]
    check("и она же в неразобранном теле", V.quota_kind(403, real_broken), "daily")
    # Проверка целиком, через проход: выпуск не должен уйти в ожидание до полуночи.
    c = with_releases(1)
    out = V.run(c, "dev", NOW,
                fetch=http_error(403, "dailyLimitExceededUnreg",
                                 "Daily Limit for Unauthenticated Use Exceeded",
                                 domain="usageLimits"),
                key="k")
    check("проход не встал ждать сброса", out["quota_wait"], 0)
    check("а честно сказал «ответа нет»", out["unknown"], 1)
    check("ожидание до полуночи не заведено", pc.quota_wait(c, now=NOW), {})


def test_engine_path_without_connection():
    print("\nБез соединения (путь движка) 403 по квоте тоже не ошибка ролика")
    res = V.check("vid1", "k", fetch=http_error(403, "quotaExceeded"))
    check("состояние — квота", res["state"], V.QUOTA)
    check("и текст говорит, что ролик ни при чём",
          "не ошибка ролика" in res["error"], True)
    check("единиц не приписано", res["cost"], 0)
    rate = V.check("vid1", "k", fetch=http_error(429, "rateLimitExceeded", "too many"))
    check("слишком часто — тоже подождать", rate["state"], V.QUOTA)
    check("но другого рода", rate["kind"], "rate")


def main() -> int:
    test_pacific_day()
    test_usage_accounting()
    test_budget_setting()
    test_wait_record()
    test_reason_words()
    test_quota_is_not_a_video_error()
    test_reason_logged_once()
    test_reset_returns_to_work()
    test_spend_is_counted()
    test_own_budget_stops_before_403()
    test_other_403_is_not_quota()
    test_reason_is_said_out_loud_every_pass()
    test_report_always_has_quota_and_why()
    test_unregistered_is_not_daily_quota()
    test_engine_path_without_connection()
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
