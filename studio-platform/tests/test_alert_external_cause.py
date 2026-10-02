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
      CREATE TABLE job_logs(id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER,
        ts REAL, line TEXT);
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

    print("\nГолый код возврата до человека не доходит")
    # Владелец: «если бы я не спросил, что случилось, я бы никак не понял, что
    # закончились кредиты». Разбирать каждую новую беду по отдельности — гонка, которую
    # не выиграть: не знаем причину — показываем, что сказал воркер.
    c = fresh()
    упала(c, 20, "prompts", "rc=2")
    for строка in (
            "▶ старт: prompts",
            "$ prompts_worker.py --project p1 …",
            "⏳ Обработка 1 текстов...",
            'У канала промптов «openai» исчерпан счёт/лимит: You have no credits remaining',
            "ℹ️ Включите режим отладки для получения детальной информации",
            # Итоговая строка самого сервера — та же пустота, что и `rc=N`: она лишь
            # повторяет код. Показать её вместо причины значит ничего не показать.
            "❌ ошибка (код 2)"):
        c.execute("INSERT INTO job_logs(job_id, ts, line) VALUES(20, 1, ?)", (строка,))
    c.commit()
    a = pub_alerts.check_failed_tasks(c, NOW, tenant=None)[0]
    check("в тревоге настоящий текст", "no credits remaining" in a.text, True)
    check("и голого кода в ней нет", "rc=2" in a.text, False)
    # Шум журнала показывать так же бесполезно, как код.
    for шум in ("Включите режим отладки", "▶ старт", "Обработка 1 текстов",
                "ошибка (код 2)"):
        check(f"шум не показан ({шум[:18]})", шум in a.text, False)

    print("\nЕсли и в журнале пусто — так и сказано")
    c = fresh()
    упала(c, 21, "tts", "rc=1")
    a = pub_alerts.check_failed_tasks(c, NOW, tenant=None)[0]
    check("сказано, что причины нет", "причина не записана" in a.text, True)
    # Код оставлен в скобках: человеку он не говорит ничего, но разбирающему пригодится.
    # Главное — что сообщение ведёт СЛОВАМИ, а не им.
    check("код только в скобках", "(rc=1)" in a.text, True)
    хвост = a.text.split("ошибкой.", 1)[-1].strip()
    check("сообщение начинается не с кода", хвост.startswith("rc="), False)
    # И выдуманной причины нет: пустоту не выдаём за диагноз.
    check("пустота названа пустотой", "не записана ни в задаче, ни в журнале" in a.text, True)

    print("\nКогда причина записана — журнал не трогаем")
    c = fresh()
    упала(c, 22, "tts", "у ролика нет субтитров (TranscriptsDisabled)")
    c.execute("INSERT INTO job_logs(job_id, ts, line) VALUES(22, 1, 'посторонняя строка')")
    c.commit()
    a = pub_alerts.check_failed_tasks(c, NOW, tenant=None)[0]
    check("показана записанная причина", "TranscriptsDisabled" in a.text, True)
    check("и журнал не подмешан", "посторонняя строка" in a.text, False)

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

    print("\nСорок одна задача на одном лежащем Lumean — одно сообщение")
    # Так это и выглядело 02.10.2026: сорок одна упавшая озвучка, причина у всех одна,
    # сорок одно сообщение в телеге. Владелец: «понятно, что если у одного проекта не
    # работает Lumean, то и в другом он тоже не работает».
    c = fresh()
    for i in range(2, 12):
        c.execute("INSERT INTO projects VALUES(?,'t1',?)", (f"p{i}", f"Проект {i}"))
    for i in range(1, 42):
        pid = f"p{(i % 10) + 2}"
        c.execute("INSERT INTO queue_tasks(job_id,tenant_id,project_id,kind,status,error,"
                  "created,finished) VALUES(?,'t1',?,'tts','error',?,?,?)",
                  (i, pid, ЛЮМЕН, NOW - 60 - i, NOW - 60 - i))
    # И одна своя беда рядом: её прятать за чужой простой нельзя ни в каком случае.
    упала(c, 99, "images", "кончились кредиты OpenAI: добавьте средства на счёт")
    c.commit()
    alerts = pub_alerts.check_failed_tasks(c, NOW, tenant=None)
    check("сообщений два, а не сорок два", len(alerts), 2)
    чужое = [a for a in alerts if "Lumean" in a.title]
    своё = [a for a in alerts if "Lumean" not in a.title]
    check("чужая беда — одна на всех", len(чужое), 1)
    check("и в ней сказано, сколько задач стоит",
          "стоит 41 задача" in чужое[0].title, True)
    check("слова провайдера приведены один раз", чужое[0].text.count("HTTP 503"), 1)
    check("названы не все проекты, а несколько", "и ещё 6" in чужое[0].text, True)
    check("сказано, что сообщение одно нарочно",
          "Одно сообщение на всех" in чужое[0].text, True)
    check("своя беда НЕ съедена чужой", len(своё), 1)
    check("и она про деньги", "кредиты" in своё[0].text, True)

    print("\nОплаченные вызовы в группе не теряются")
    # Опасность складывания — соврать про деньги: если хоть по одной задаче группы
    # вызов уже оплачен, молчать об этом нельзя.
    pub_calls.reserve(c, tenant_id="t1", project_id="p3", kind="tts",
                      unit="весь текст", fp="отп1", provider="lumean", now=NOW - 600)
    c.execute("UPDATE provider_calls SET status='delivered', cost=12 WHERE project_id='p3'")
    c.commit()
    чужое = [a for a in pub_alerts.check_failed_tasks(c, NOW, tenant=None)
             if "Lumean" in a.title][0]
    check("про оплату сказано прямо", "оплаченные вызовы ЕСТЬ" in (чужое.money or ""), True)
    check("и названо, где смотреть", "Проект 3" in (чужое.money or ""), True)

    print("\nДве беды с одинаковым началом не съедают друг друга")
    # Ключ тревоги режется по длине, чтобы не раздувать состояние. Если бы он был просто
    # обрезанным текстом, две причины с общим длинным началом получили бы ОДИН ключ — и
    # вторую `tick` счёл бы повтором первой, то есть молча проглотил бы целую беду.
    c = fresh()
    общее = "этап остановился на шаге подготовки материалов, подробности такие: "
    for jid, хвост in ((1, "не найден шрифт"), (2, "не найден шрифт"),
                       (3, "не хватило места"), (4, "не хватило места")):
        упала(c, jid, "render", общее + хвост)
    беды = pub_alerts.check_failed_tasks(c, NOW, tenant=None)
    check("тревоги две, а не одна", len(беды), 2)
    # Одинаковый ключ = вторую тревогу `tick` счёл бы повтором первой и не отправил.
    check("и ключи у них разные", len({a.key for a in беды}), 2)
    check("в одной сказано про шрифт",
          len([a for a in беды if "шрифт" in a.text]), 1)
    check("в другой — про место",
          len([a for a in беды if "места" in a.text]), 1)

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
