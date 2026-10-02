#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тревога называет ЧУЖУЮ беду чужой (02.10.2026).

Владелец: «в телегу всё ещё приходит тревога типа что у нас сломалось, но нужно кидать,
что Lumean недоступен или у нас исчерпалась квота по запросам к YouTube, чтобы было
больше контекста».

Разница не косметическая. «Задача упала» поднимает человека и отправляет искать поломку
у себя. «Lumean недоступен — ждём» говорит, что чинить нечего. Перепутать их значит
потратить вечер на поиск того, чего нет.

Проверяется и обратное, что важнее: СВОЯ беда чужой назваться не должна. Нет ключа,
кончились деньги, оборван конвейер — это к нам, и мягкая формулировка «ждём, пока
поднимется» тут прямо вредна.

    .venv/bin/python tests/test_alert_external_cause.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_alerts                                                  # noqa: E402
import pub_calls                                                   # noqa: E402

NOW = 1_800_000_000.0
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fresh():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT, project_id TEXT,
        kind TEXT, status TEXT, error TEXT, created REAL, finished REAL);
      CREATE TABLE projects(id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT);
    """)
    pub_calls.ensure_calls_schema(c)
    c.execute("INSERT INTO projects VALUES('p1','t1','Лесные байки')")
    c.commit()
    return c


def упала(c, jid, kind, error):
    c.execute("INSERT INTO queue_tasks(job_id,tenant_id,project_id,kind,status,error,"
              "created,finished) VALUES(?,'t1','p1',?,?,?,?,?)",
              (jid, kind, "error", error, NOW - 60, NOW - 60))
    c.commit()


ЛЮМЕН = ("сервис провайдера не поднялся за 3 попыток: Lumean: /orders: HTTP 503: "
         "Сервис временно недоступен: идут технические работы")


def main() -> int:
    print("Чужая беда узнаётся и называется по имени")
    случаи = [
        (ЛЮМЕН, "Lumean"),
        ("субтитры: все выходы исчерпали квоту, ближайший через 29 мин", "YouTube"),
        ("FastGen: HTTP 503 Service Unavailable", "FastGen"),
        ("Codex: HTTP 502 Bad Gateway", "Codex"),
        ("Claude: service unavailable", "Claude"),
        ("HTTP 504 gateway timeout", "внешний сервис"),
    ]
    for текст, кто in случаи:
        check(f"узнан «{кто}»", pub_alerts.внешняя_беда(текст)[0], кто)
        # Наружу уходят СЛОВА ПРОВАЙДЕРА, а не наш пересказ.
        check(f"и переданы его слова ({кто})",
              pub_alerts.внешняя_беда(текст)[1][:30], " ".join(текст.split())[:30])

    print("\nСвоя беда чужой не притворяется")
    своё = [
        ("rc=1", "голый код возврата"),
        ("конвейер оборван: последняя задача промптов не завершилась успешно", "оборван конвейер"),
        ("ключ sk-proj-… отказ авторизации", "наш ключ"),
        ("нет ключа для генерации названия", "не настроено"),
        # Рерайт — наш, а не чужой сервис: назвать его виновником значит отправить
        # человека искать не там (правило «одна студия»).
        ("рерайт вернул пустой список моделей", "наш же рерайт"),
        ("", "пусто"),
    ]
    for текст, почему in своё:
        check(f"не выдано за чужое ({почему})", pub_alerts.внешняя_беда(текст)[0], "")

    print("\nДеньги и ключи — наша беда, даже когда рядом слово «квота»")
    # Самая дорогая ошибка в эту сторону: сказать «ждём, пока поднимется» на пустом
    # счёте. Это сутки простоя вместо пополнения за минуту.
    денежные = [
        ('У канала промптов «openai» исчерпан счёт/лимит: You have no credits '
         'remaining. Add credits to continue using the API', "пустой счёт OpenAI"),
        ("HTTP 429: quota exceeded — insufficient balance", "квота рядом с деньгами"),
        ("ключ отвергнут: unauthorized", "отказ ключа"),
        ("billing: payment required", "оплата"),
    ]
    for текст, почему in денежные:
        check(f"осталась нашей ({почему})", pub_alerts.внешняя_беда(текст)[0], "")

    print("\nСледствие не поднимает вторую тревогу")
    # «Конвейер оборван» — тень уже названной беды. На тридцати проектах из одной
    # поломки выходило шестьдесят сообщений.
    c = fresh()
    упала(c, 10, "prompts", "У канала промптов «openai» исчерпан счёт/лимит: no credits")
    упала(c, 11, "images", "конвейер оборван: последняя задача промптов "
                           "не завершилась успешно (status=error)")
    тревоги = pub_alerts.check_failed_tasks(c, NOW, tenant=None)
    check("тревога одна, а не две", len(тревоги), 1)
    check("и это причина, а не следствие", "prompts" in тревоги[0].title, True)
    check("в ней видно, что кончились деньги",
          "счёт" in тревоги[0].text or "credits" in тревоги[0].text, True)

    print("\nТревога целиком: заголовок и тело")
    c = fresh()
    упала(c, 1, "tts", ЛЮМЕН)
    тревоги = pub_alerts.check_failed_tasks(c, NOW, tenant=None)
    check("тревога одна", len(тревоги), 1)
    a = тревоги[0]
    check("в заголовке назван виновник", "Lumean недоступен" in a.title, True)
    check("и слова «задача упала» в нём нет", "Задача упала" in a.title, False)
    check("в теле сказано, что это не мы", "не наша поломка" in a.text, True)
    check("и что делать нечего", "Чинить нечего" in a.text, True)
    check("и приведён ответ провайдера", "HTTP 503" in a.text, True)
    check("этап назван", "«tts»" in a.text, True)
    check("проект назван", "Лесные байки" in a.text, True)
    check("деньги посчитаны как раньше", "не записано" in (a.money or ""), True)

    print("\nСвоя поломка подаётся как раньше — тревожно")
    c = fresh()
    упала(c, 2, "images", "rc=3")
    a = pub_alerts.check_failed_tasks(c, NOW, tenant=None)[0]
    check("заголовок прежний", a.title, "Задача упала: images")
    check("и в теле нет успокоительного", "Чинить нечего" in a.text, False)

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
