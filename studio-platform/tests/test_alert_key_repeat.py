#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Поводы про ключи повторяются раз в сутки, остальные — как раньше (28.09.2026).

Владелец: «можем сделать так, чтобы нотификация в телегу именно касательно ключей была
раз в день, а то сейчас очень много раз долбит прилажка».

Суть правки — не в числе, а в разделении пород повода:

  • ключ истекает / кончился / подписка разлогинилась / провайдер отдыхает — это
    состояния, которые НЕ чинятся за час ничем. Час между напоминаниями об одном и том
    же сроке не добавляет ни одного нового сведения, зато к третьему разу тревоги
    перестают читать — и пропускают ту, которая про очередь;
  • очередь встала, заливки нет, кончается диск — чинятся быстро, и там частое
    напоминание и есть польза. Их трогать нельзя.

ПЕРВОЕ сообщение не задерживается никогда: реже становятся ПОВТОРЫ.

    .venv/bin/python tests/test_alert_key_repeat.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="alert-key-repeat-")
os.environ["VS_DEV_DB"] = os.path.join(TMP, "vsgen.db")
os.environ["VS_SERVER_DB"] = os.environ["VS_DEV_DB"]
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_alerts as A                                            # noqa: E402

FAILS = []
HOUR = 3600.0


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def main() -> int:
    print("Каждому поводу свой срок повтора")
    day = A.KEY_REPEAT_SEC
    check("про ключи — сутки", day, 24 * HOUR)
    check("про остальное — час", A.REPEAT_SEC, HOUR)

    print("Поводы про ключи ждут сутки")
    for key in ("fastgen_key_expiring", "fastgen_usage_near_limit",
                "key_dead:googler", "key_exhausted:openai", "text_sub_dead:abc123",
                "provider_resting:elevenlabs"):
        check(f"{key} — сутки", A.repeat_after(key), day)

    print("Поводы про конвейер — как раньше")
    # Эти чинятся за минуты, и частое напоминание о них — польза, а не шум. Если правка
    # зацепит их, конвейер будет молча стоять сутки.
    for key in ("queue_stalled", "upload_stalled", "failed:17"):
        check(f"{key} — час", A.repeat_after(key), A.REPEAT_SEC)

    print("Хронические состояния — сутки (17.10.2026, владелец: «без воды»)")
    # «Слот прошёл, ролика нет» за 14 суток ушло 358 раз — по разу в час про одно и то же.
    # Такие поводы не чинятся за час; напоминание раз в сутки, а раньше — только если
    # цифра выросла (см. ниже).
    for key in ("oldest_task", "disk_low", "disk_low_node:minsk", "slot_missed",
                "slot_empty", "upload_profile_busy", "channel_supply"):
        check(f"{key} — сутки", A.repeat_after(key), A.CHRONIC_REPEAT_SEC)
    check("хронический срок — сутки", A.CHRONIC_REPEAT_SEC, 24 * HOUR)

    print("Семейство в ключе не мешает узнать повод")
    check("ключ с двоеточием разбирается по префиксу",
          A.repeat_after("key_dead:какое-угодно:семейство"), day)

    print("Порог правится с экрана, как остальные")
    check("заведён в списке порогов", "KEY_REPEAT_SEC" in A.LIMITS, True)
    name, unit, lo, hi = A.LIMITS["KEY_REPEAT_SEC"]
    check("измеряется во времени", unit, "sec")
    check("сутки помещаются в границы", lo <= day <= hi, True)
    check("и неделя тоже — на случай «совсем редко»", hi >= 7 * 86400, True)

    print("Правка с экрана применяется к повтору")
    c = sqlite3.connect(os.environ["VS_DEV_DB"])
    c.execute("CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, "
              "updated REAL)")
    c.commit()
    A.set_limit(c, "KEY_REPEAT_SEC", 3 * 24 * HOUR)
    A.load_limits(c)
    check("повтор стал трёхсуточным", A.repeat_after("key_dead:googler"), 3 * 24 * HOUR)
    check("а конвейерный не тронулся", A.repeat_after("queue_stalled"), A.REPEAT_SEC)
    A.set_limit(c, "KEY_REPEAT_SEC", None)
    A.load_limits(c)
    check("возврат к тому, что на машине", A.repeat_after("key_dead:googler"), day)
    c.close()

    print("Живой проход: ключ молчит сутки, очередь напоминает через час")
    # Проверяется не функция, а сам проход подачи: подменяем набор проверок на два
    # рукотворных повода и считаем, что ушло в телеграм на каждом тике.
    saved_checks = A.CHECKS
    def fake(c_, now_, tenant_):
        return [A.Alert("key_dead:googler", "Ключ FastGen кончился", "текст", now_ - 10 * HOUR),
                A.Alert("queue_stalled", "Очередь встала", "текст", now_ - 10 * HOUR)]
    A.CHECKS = (fake,)
    try:
        c = sqlite3.connect(os.environ["VS_DEV_DB"])
        c.execute("DELETE FROM admin_state")
        c.commit()
        A.load_limits(c)
        t0 = 1790000000.0
        said = {}
        def run(at):
            out = A.tick(c, lambda text: True, now=at)
            for key, kind, _text in out:
                said.setdefault(key, []).append((at, kind))
            return out

        first = run(t0)
        check("первый раз звучат оба", len(first), 2)
        check("и оба как тревога", sorted(k for _k, k, _t in first), ["alarm", "alarm"])

        # Через час с минутой: конвейерный повод повторяется, ключ — молчит.
        run(t0 + HOUR + 60)
        check("очередь напомнила", len(said.get("queue_stalled", [])), 2)
        check("ключ промолчал", len(said.get("key_dead:googler", [])), 1)

        # Ещё пять часов той же минутной долбёжкой: ключ по-прежнему молчит.
        for step in range(1, 6):
            run(t0 + HOUR + 60 + step * HOUR)
        check("ключ молчал все шесть часов", len(said.get("key_dead:googler", [])), 1)
        check("а очередь напоминала каждый час",
              len(said.get("queue_stalled", [])), 7)


        # Через сутки с минутой — ключ напомнит один раз.
        run(t0 + 24 * HOUR + 60)
        check("через сутки ключ напомнил", len(said.get("key_dead:googler", [])), 2)
        check("и это повтор, а не новая тревога",
              said["key_dead:googler"][-1][1], "repeat")

        print("Хронический повод: молчит сутки, но выросшая цифра — новость")
        c.execute("DELETE FROM admin_state"); c.commit(); A.load_limits(c)
        figure = {"n": 5}
        def chronic(c_, now_, tenant_):
            return [A.Alert("slot_missed", "Слот прошёл, ролика нет", "текст",
                            now_ - 10 * HOUR, figure=figure["n"])]
        A.CHECKS = (chronic,)
        said.clear()
        run(t0)
        run(t0 + HOUR + 60)
        check("через час при той же цифре — тишина", len(said.get("slot_missed", [])), 1)
        figure["n"] = 9
        run(t0 + HOUR + 120)
        check("цифра выросла — сказали, не дожидаясь суток",
              len(said.get("slot_missed", [])), 2)
        figure["n"] = 11
        run(t0 + HOUR + 180)
        check("но не чаще общего часа", len(said.get("slot_missed", [])), 2)
        figure["n"] = 3
        run(t0 + 26 * HOUR)
        check("цифра упала — ждали сутки, потом напомнили",
              len(said.get("slot_missed", [])), 3)
        c.close()
    finally:
        A.CHECKS = saved_checks

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
