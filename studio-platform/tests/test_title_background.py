#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Названия пишутся в фоне и не останавливают круг движка (04.10.2026).

Подписка Codex — одна на все службы машины; старый кабинет держал большую часть мест, и
название шло 4–8 минут. Внутри круга движка это останавливало все остальные выпуски: у
кого срок фазы короче, засчитывали попытку каждый круг, не дойдя до своего обработчика.
Бюджет времени на круг (прошлая попытка) не помогал: один вызов длиннее любого бюджета.

Что проверяется:
  • круг заказывает название и ждёт не больше пары секунд, даже если подписка молчит;
  • быстрый ответ или быстрый отказ забирается в том же круге;
  • пока пишется — выпуск ждёт с понятной причиной; готово — забирается;
  • одновременно пишется не больше TITLE_WORKERS названий, остальные — по очереди;
  • временный отказ — выпуск ждёт и следующий круг заказывает заново;
  • отказ по вине выпуска (нет исходного названия) — не «ждём», а ошибка выпуска;
  • ключ читается в круге движка, а не в фоне (соединение с базой принадлежит кругу);
  • путь OpenAI остаётся прежним; движок по-прежнему зовёт begin_tick.

    .venv/bin/python tests/test_title_background.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_engine as E                                            # noqa: E402
import pub_schema                                                 # noqa: E402
import pub_services as S                                          # noqa: E402
from pub_publish import TitleError, TitleUnavailable              # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def rel(rid):
    return {"release_id": rid, "donor_title": "The Silent Sky", "lang": "cs"}


def svc():
    s = S.PubServices({"create_project": lambda *a, **k: "", "enqueue": lambda *a, **k: None,
                       "project_dir": lambda *a, **k: "/нет"})
    s.TITLE_GRACE_SEC = 0.2
    return s


def attempt(s, r):
    """(что вернулось, сколько секунд занял вызов)."""
    t = time.monotonic()
    try:
        out = ("title", s._make_title(None, r))
    except TitleUnavailable as exc:
        out = ("wait", str(exc))
    except TitleError as exc:
        out = ("error", str(exc))
    return out, time.monotonic() - t


def settle(s, rid, limit=5.0):
    t = time.monotonic()
    while time.monotonic() - t < limit:
        with s._titles_lock:
            job = s._titles.get(rid)
            if job is None or not job["thread"].is_alive():
                return
        time.sleep(0.01)


def main() -> int:
    saved = (S.make_title_codex, S.make_title, S.get_key)
    gate = threading.Event()
    key_threads = []

    def slow_codex(donor, lang, key, base, model=""):
        gate.wait(10)
        return f"Tiché nebe ({key})"

    S.make_title_codex = slow_codex
    S.get_key = lambda c, name: key_threads.append(threading.current_thread().name) or "k"
    os.environ.pop("VS_TITLE_PROVIDER", None)
    try:
        print("Круг заказывает название и не ждёт подписку")
        s = svc()
        (kind, msg), took = attempt(s, rel("r1"))
        check("выпуск ждёт", kind, "wait")
        check("причина: заказал", "заказал название" in msg, True)
        check("вызов вернулся за доли секунды, а подписка ещё пишет", took < 1.0, True)
        check("ключ прочитан в круге, а не в фоне", key_threads, [threading.current_thread().name])
        (kind, msg), _ = attempt(s, rel("r1"))
        check("пока пишется — ждём с понятной причиной", (kind, "пишется" in msg), ("wait", True))

        print("\n  не больше двух одновременно")
        attempt(s, rel("r2"))
        (kind, msg), took = attempt(s, rel("r3"))
        check("третий — по очереди", (kind, "по очереди" in msg), ("wait", True))
        check("и третий не заказан", "r3" in s._titles, False)

        print("\n  готово — забираем")
        gate.set()
        settle(s, "r1")
        (kind, msg), _ = attempt(s, rel("r1"))
        check("название получено", (kind, msg), ("title", "Tiché nebe (k)"))
        check("запись о заказе убрана", "r1" in s._titles, False)

        print("\nВременный отказ — ждём и заказываем заново")
        def busy(*a, **k):
            raise TitleUnavailable("подписка Codex не дала название (502): HOST_BUDGET_WAIT")
        S.make_title_codex = busy
        s = svc()
        (kind, msg), _ = attempt(s, rel("r4"))
        check("быстрый отказ виден выпуску в том же круге", (kind, "502" in msg), ("wait", True))
        check("заказ убран — следующий круг закажет заново", "r4" in s._titles, False)

        print("\n  быстрый ответ — в том же круге")
        S.make_title_codex = lambda *a, **k: "Rychlý název"
        s = svc()
        (kind, msg), _ = attempt(s, rel("r8"))
        check("название сразу", (kind, msg), ("title", "Rychlý název"))
        check("и заказ не висит", "r8" in s._titles, False)

        def quick(*a, **k):
            time.sleep(0.1)
            return "Za desetinu sekundy"
        S.make_title_codex = quick
        s = svc()
        (kind, msg), _ = attempt(s, rel("r9"))
        check("ответ за 0,1 с при окне 0,2 с — тоже в том же круге", (kind, msg),
              ("title", "Za desetinu sekundy"))
        S.make_title_codex = busy

        print("\nОтказ по вине выпуска — ошибка, а не ожидание")
        def bad(*a, **k):
            raise TitleError("у донора нет названия — не из чего делать заголовок")
        S.make_title_codex = bad
        s = svc()
        (kind, msg), _ = attempt(s, rel("r5"))
        check("ошибка выпуска", (kind, "нет названия" in msg), ("error", True))

        print("\nВ плане названия нет — спрашиваем донора ещё раз и запоминаем (14.10.2026)")
        import pub_tags
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE pub_plan_items(id INTEGER PRIMARY KEY, donor_title TEXT)")
        c.execute("INSERT INTO pub_plan_items(id,donor_title) VALUES(605,'')")
        got = []
        S.make_title_codex = lambda donor, *a, **k: got.append(donor) or "Mrazivý název"
        saved_lookup = pub_tags.donor_lookup
        pub_tags.donor_lookup = lambda url, c_=None: ("alive", "Провёл 66 ЛЕТ в капсуле")
        try:
            s = svc()
            r = {"release_id": "r9", "donor_title": "", "lang": "cs", "plan_item_id": 605,
                 "donor_url": "https://youtu.be/a0z3FKQ_5rI"}
            t0 = time.monotonic()
            out = None
            while time.monotonic() - t0 < 3:
                try:
                    out = s._make_title(c, r)
                    break
                except TitleUnavailable:
                    time.sleep(0.05)
            check("название сделано из найденного", (out, got[:1]),
                  ("Mrazivý název", ["Провёл 66 ЛЕТ в капсуле"]))
            check("и записано в план", c.execute(
                "SELECT donor_title FROM pub_plan_items WHERE id=605").fetchone()[0],
                "Провёл 66 ЛЕТ в капсуле")
            pub_tags.donor_lookup = lambda url, c_=None: ("gone", "")
            S.make_title_codex = bad
            s = svc()
            (kind, msg), _ = attempt(s, {**r, "release_id": "r10", "plan_item_id": 1})
            check("донора нет — по-прежнему ошибка выпуска", (kind, "нет названия" in msg),
                  ("error", True))
        finally:
            pub_tags.donor_lookup = saved_lookup

        print("\nНеожиданное исключение в фоне — ждём, а не роняем круг")
        def boom(*a, **k):
            raise ValueError("сломалось")
        S.make_title_codex = boom
        s = svc()
        (kind, msg), _ = attempt(s, rel("r6"))
        check("ожидание с типом ошибки", (kind, "ValueError" in msg), ("wait", True))

        print("\nПуть OpenAI — прежний, синхронный")
        os.environ["VS_TITLE_PROVIDER"] = "openai"
        S.make_title = lambda *a, **k: "Название OpenAI"
        s = svc()
        s._openai_key = lambda c: "sk"
        (kind, msg), _ = attempt(s, rel("r7"))
        check("сразу название", (kind, msg), ("title", "Название OpenAI"))
        os.environ.pop("VS_TITLE_PROVIDER", None)

        print("\nДвижок зовёт begin_tick, как раньше")
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        pub_schema.ensure_pub_schema(c)
        seen = []

        class Stub:
            def begin_tick(self, now):
                seen.append(now)
        E.tick(c, Stub(), now=1_800_000_000.0)
        check("begin_tick вызван", seen, [1_800_000_000.0])
        check("без begin_tick движок работает", isinstance(E.tick(c, object(), now=1.0), dict), True)
        check("по умолчанию два фоновых названия", S.PubServices.TITLE_WORKERS, 2)
        check("быстрый ответ ждём 2 с", S.PubServices.TITLE_GRACE_SEC, 2.0)
    finally:
        gate.set()
        S.make_title_codex, S.make_title, S.get_key = saved
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
