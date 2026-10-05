#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Потерянный провайдером заказ не держит остальные оплаченные кадры (03.10.2026).

Что случилось. Два кадра из 609 висели в журнале как `ordered` 14 и 17 часов: заказы
были созданы и оплачены, результата не пришло, а на вопрос о них провайдер отвечал
404 — он чистит старые операции. Прежняя развилка считала 404 НЕОДНОЗНАЧНОСТЬЮ, поэтому
каждый прогон шёл спрашивать и получал тот же 404. Вечный круг.

Цена круга: рядом лежали 607 ОПЛАЧЕННЫХ и полученных кадров, которые не проходили
контракт квитанций из-за одного недостающего и не ехали в рендер. То есть правило
берегло один потерянный заказ ценой шестисот полученных.

Теперь у журнала есть третий случай — `lost`. Он отличается от обоих прежних именно
судьбой денег, и это различие в тесте закреплено: «не платили» (`ordered=False`),
«заплатили, судьба неизвестна» (`ordered=True`), «заплатили впустую» (`lost=True`).

Побочно проверяется, что перезаказ после `lost` идёт ОБЫЧНЫМ путём генерации — тем, где
живёт смягчатель промпта. Это закрывает вторую половину беды: отказ фильтра
содержания, пришедший в статусе заказа, прежде до смягчателя не доходил вовсе.

    .venv/bin/python tests/test_lost_order.py
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_calls as PC                                            # noqa: E402
import pub_calls_api                                              # noqa: E402

NOW = 1_800_000_000.0
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fresh(path=":memory:"):
    c = sqlite3.connect(path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    PC.ensure_calls_schema(c)
    return c


def с_задачей(tmp, имя):
    """База на диске: ручку зовёт TestClient из другого потока, а соединение
    sqlite к потоку привязано. В бою каждый запрос открывает своё соединение —
    здесь делаем так же."""
    путь = os.path.join(tmp, имя)
    c = fresh(путь)
    c.executescript("""
      CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
                               project_id TEXT, kind TEXT, status TEXT,
                               claim_token TEXT);
    """)
    c.execute("INSERT INTO queue_tasks VALUES(7,'t1','p1','images','running','tok1')")
    c.commit()
    return путь, c


def заказать(c, unit="045", fp="отп1"):
    return PC.reserve(c, tenant_id="t1", project_id="p1", kind="images",
                      unit=unit, fp=fp, provider="recrafter", now=NOW)


def состояние(c, call_id):
    return c.execute("SELECT status, error, attempts FROM provider_calls WHERE id=?",
                     (call_id,)).fetchone()


def main() -> int:
    print("Три исхода неудачи различаются судьбой денег")

    print("\n  не платили (ordered=False): повтор свободен")
    c = fresh()
    d = заказать(c)
    PC.mark_failed(c, d["call_id"], "429 до создания заказа", ordered=False, now=NOW)
    check("строка в failed", состояние(c, d["call_id"])["status"], "failed")
    check("и следующая попытка заказывает заново", заказать(c)["action"], "new")

    print("\n  заплатили, судьба неизвестна (ordered=True): спрашиваем, не платим")
    c = fresh()
    d = заказать(c)
    PC.mark_ordered(c, d["call_id"], "gen_1", now=NOW)
    PC.mark_failed(c, d["call_id"], "таймаут опроса", ordered=True, now=NOW)
    check("строка осталась в ordered", состояние(c, d["call_id"])["status"], "ordered")
    check("и следующая попытка идёт СПРАШИВАТЬ", заказать(c)["action"], "verify")
    check("ошибка записана", "таймаут" in состояние(c, d["call_id"])["error"], True)

    print("\n  заплатили впустую (lost=True): повтор разрешён осознанно")
    c = fresh()
    d = заказать(c)
    PC.mark_ordered(c, d["call_id"], "gen_61c36", now=NOW)
    PC.mark_failed(c, d["call_id"], "провайдер потерял заказ gen_61c36 (404)",
                   ordered=True, lost=True, now=NOW)
    строка = состояние(c, d["call_id"])
    check("строка ушла в failed", строка["status"], "failed")
    check("в ошибке сказано, что заказ потерян", "потерял" in строка["error"], True)
    check("и код провайдера сохранён", "404" in строка["error"], True)
    снова = заказать(c)
    check("следующая попытка ЗАКАЗЫВАЕТ, а не спрашивает", снова["action"], "new")
    # Это и есть вход в обычный путь генерации — тот, где живёт смягчатель промпта.
    check("и это та же строка, а не вторая", снова["call_id"], d["call_id"])
    check("попытка посчитана", состояние(c, d["call_id"])["attempts"], 2)

    print("\n  lost без ordered ведёт себя так же: деньги уже не вернуть")
    c = fresh()
    d = заказать(c)
    PC.mark_failed(c, d["call_id"], "404", ordered=False, lost=True, now=NOW)
    check("строка в failed", состояние(c, d["call_id"])["status"], "failed")

    print("\nПолученный кадр не трогается ничем из этого")
    c = fresh()
    d = заказать(c)
    PC.mark_ordered(c, d["call_id"], "gen_2", now=NOW)
    PC.mark_delivered(c, d["call_id"], "/p1/frames/045.jpg", now=NOW)
    check("строка delivered", состояние(c, d["call_id"])["status"], "delivered")
    check("и повторно его не заказывают", заказать(c)["action"], "skip")

    print("\nРучка журнала пропускает признак до базы")
    # Без этой проверки мутация «убрать lost из ручки» оставалась невидимой: признак
    # живёт в воркере и в базе, а между ними ещё HTTP, и он тоже часть починки.
    import tempfile
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    with tempfile.TemporaryDirectory() as tmp:
        путь, c = с_задачей(tmp, "a.db")
        d = заказать(c)
        PC.mark_ordered(c, d["call_id"], "gen_61c36", now=NOW)
        c.commit()
        app = FastAPI()
        pub_calls_api.register_calls_routes(
            app, {"db": lambda: fresh(путь), "_now": lambda: NOW})
        ответ = TestClient(app).post(f"/api/calls/{d['call_id']}/failed", json={
            "job_id": 7, "job_token": "tok1", "ordered": True, "lost": True,
            "error": "провайдер потерял заказ gen_61c36 (404)"})
        check("ручка ответила ok", ответ.status_code, 200)
        проверка = fresh(путь)
        check("и строка ушла в failed, а не осталась в ordered",
              проверка.execute("SELECT status FROM provider_calls WHERE id=?",
                               (d["call_id"],)).fetchone()["status"], "failed")

        # Та же ручка БЕЗ lost обязана вести себя как прежде — иначе починка сломала бы
        # защиту от второй оплаты для обычного таймаута.
        путь2, c2 = с_задачей(tmp, "b.db")
        d2 = заказать(c2)
        PC.mark_ordered(c2, d2["call_id"], "gen_2", now=NOW)
        c2.commit()
        app2 = FastAPI()
        pub_calls_api.register_calls_routes(
            app2, {"db": lambda: fresh(путь2), "_now": lambda: NOW})
        TestClient(app2).post(f"/api/calls/{d2['call_id']}/failed", json={
            "job_id": 7, "job_token": "tok1", "ordered": True, "error": "таймаут"})
        check("без lost строка по-прежнему ждёт опроса",
              fresh(путь2).execute("SELECT status FROM provider_calls WHERE id=?",
                                   (d2["call_id"],)).fetchone()["status"], "ordered")

    print("\nВоркер действительно зовёт журнал на 404")
    # Поведенческую проверку сюда не затащить: ветка живёт внутри генерации кадра, за
    # пулом потоков и живым провайдером. Поэтому — проверка по исходнику, и она честно
    # слабее: ловит удаление вызова, но не докажет, что он срабатывает в бою. Нужна
    # потому, что мутация «убрать вызов» не гасила НИ ОДНУ другую проверку.
    src = io.open(os.path.join(BASE, "workers", "googler_worker.py"),
                  encoding="utf-8").read()
    ветка = src[src.find('_jrn_why = ""'):src.find('_jrn_why = ""') + 2000]
    # 05.10.2026 опознание вынесено в order_never_delivers (там же — отклонённый заказ,
    # status=failed); поведение helper'а проверяет tests/test_order_never_delivers.py.
    check("404 в ветке опознаётся",
          "order_never_delivers(_jrn_probe)" in ветка
          and '"404" in err' in src[src.find("def order_never_delivers"):
                                     src.find("def order_never_delivers") + 1500], True)
    check("и журналу говорится именно lost", "lost=True" in ветка, True)
    check("вместе с ordered=True — деньги-то ушли", "ordered=True, lost=True" in ветка, True)
    check("и человеку сказано, что перезаказ дешевле",
          "дешевле" in ветка, True)

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
