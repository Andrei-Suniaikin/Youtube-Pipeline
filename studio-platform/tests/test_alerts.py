#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тревоги: шесть поводов и три правила подачи.

Сети здесь нет вообще: `pub_alerts` отправку не делает, а транспорт подменён списком.
Поэтому тест гоняется где угодно, включая машину без телеграма и без интернета.

    .venv/bin/python tests/test_alerts.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

# Пороги фиксируем ДО импорта: модуль читает их один раз, как и в бою.
os.environ.setdefault("VS_ALERT_DISK_GB", "0")     # диск в тесте не повод
os.environ.setdefault("VS_TENANTS_DIR", BASE)
import pub_alerts as A                                            # noqa: E402

NOW = 1_800_000_000.0
HOUR = 3600.0

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def fresh() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript("""
      CREATE TABLE admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
      CREATE TABLE queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT, kind TEXT,
                               status TEXT, created REAL, finished REAL);
      CREATE TABLE pub_upload_jobs(id INTEGER PRIMARY KEY, tenant_id TEXT, status TEXT,
                                   created REAL, finished REAL);
      CREATE TABLE pub_releases(id INTEGER PRIMARY KEY, tenant_id TEXT, plan_item_id INT,
                                slot_at REAL, phase TEXT, status TEXT);
      CREATE TABLE pub_plan_items(id INTEGER PRIMARY KEY, tenant_id TEXT, slot_at REAL,
                                  status TEXT);
    """)
    return c


class Bag:
    """Транспорт-заглушка. `ok=False` — телеграм лежит."""

    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    def __call__(self, text):
        if not self.ok:
            return False
        self.sent.append(text)
        return True

    def last(self):
        return self.sent[-1] if self.sent else ""


def keys(c, now=NOW):
    return sorted(A.detect(c, now).keys())


def main() -> int:
    print("Повод 1 — очередь встала")
    c = fresh()
    c.execute("INSERT INTO queue_tasks VALUES(1,'dev','images','running',?,NULL)",
              (NOW - 10 * 60,))
    check("свежая задача поводом не является", keys(c), [])
    c.execute("UPDATE queue_tasks SET created=?", (NOW - 40 * 60,))
    check("ждёт дольше порога и ничего не завершалось", keys(c), ["queue_stalled"])
    c.execute("INSERT INTO queue_tasks VALUES(2,'dev','tts','done',?,?)",
              (NOW - 60 * 60, NOW - 5 * 60))
    check("недавнее завершение снимает повод", keys(c), [])

    print("\nПовод 2 — провайдер на паузе")
    c = fresh()
    c.execute("INSERT INTO admin_state VALUES('credential_cooldown',"
              "'{\"googler:1\":{\"family\":\"googler\",\"mask\":\"fg_l…b3gt\","
              "\"until\":%d,\"why\":\"HTTP 429\"}}',0)" % int(NOW + 10 * 60))
    check("короткая пауза — повод есть", keys(c), ["provider_resting:googler"])
    bag = Bag()
    A.tick(c, bag, now=NOW)
    check("но говорить о ней рано: держим час", bag.sent, [])
    # Так выглядит настоящая долгая пауза: каждый следующий отказ продлевает её ещё
    # на четверть часа, и вперёд она никогда не видна дальше пятнадцати минут.
    # Значит длину доказывает только наше собственное наблюдение.
    def extend(to):
        c.execute("UPDATE admin_state SET v=? WHERE k='credential_cooldown'",
                  ('{"googler:1":{"family":"googler","mask":"fg_l…b3gt",'
                   '"until":%d,"why":"HTTP 429"}}' % int(to),))
    extend(NOW + 40 * 60)
    A.tick(c, bag, now=NOW + 30 * 60)
    check("через полчаса всё ещё рано", bag.sent, [])
    extend(NOW + HOUR + 15 * 60)
    A.tick(c, bag, now=NOW + HOUR + 60)
    check("продержалась час — сказали", len(bag.sent), 1)
    check("названа семья ключа", "googler" in bag.last(), True)
    check("значение ключа не утекло", "fg_live" in bag.last(), False)

    print("\n  пауза, заведомо длиннее часа, объявляется сразу")
    c = fresh()
    c.execute("INSERT INTO admin_state VALUES('credential_cooldown',"
              "'{\"googler:1\":{\"family\":\"googler\",\"mask\":\"m\",\"until\":%d,"
              "\"why\":\"суточный лимит\"}}',0)" % int(NOW + 4 * HOUR))
    bag = Bag()
    A.tick(c, bag, now=NOW)
    check("ждать доказательства незачем", len(bag.sent), 1)

    print("\nПовод 3 — заливка встала")
    c = fresh()
    c.execute("INSERT INTO pub_upload_jobs VALUES(1,'dev','queued',?,NULL)",
              (NOW - 4 * HOUR,))
    check("готовый комплект ждёт дольше трёх часов", keys(c), ["upload_stalled"])
    c.execute("INSERT INTO pub_upload_jobs VALUES(2,'dev','done',?,?)",
              (NOW - 5 * HOUR, NOW - HOUR))
    check("час назад что-то залилось — не повод", keys(c), [])

    print("\nПовод 4 — слот прошёл")
    c = fresh()
    c.execute("INSERT INTO pub_releases VALUES(1,'dev',7,?,'media_ready','running')",
              (NOW - HOUR,))
    check("выпуск не доехал до слота", keys(c), ["slot_missed"])
    c.execute("UPDATE pub_releases SET phase='scheduled'")
    check("Studio приняла — повода нет", keys(c), [])
    c = fresh()
    c.execute("INSERT INTO pub_plan_items VALUES(7,'dev',?,'planned')", (NOW - HOUR,))
    check("слот в плане без выпуска — отдельный повод", keys(c), ["slot_empty"])
    check("и он про планирование, а не про деньги",
          "Денег не потрачено" in A.detect(c, NOW)["slot_empty"].money, True)

    print("\nПовод 6 — задача ждёт слишком долго")
    c = fresh()
    c.execute("INSERT INTO queue_tasks VALUES(1,'dev','images','queued',?,NULL)",
              (NOW - 7 * HOUR,))
    check("семь часов в очереди", "oldest_task" in keys(c), True)
    c.execute("UPDATE queue_tasks SET status='running'")
    check("начатая задача сюда не считается", "oldest_task" in keys(c), False)

    print("\nМесто на машинах рендера и заливки")
    c = fresh()
    from pub_calls import ensure_calls_schema, note_node_disk
    ensure_calls_schema(c)
    note_node_disk(c, "minsk", "upload", free_gb=3.0, total_gb=500.0, now=NOW)
    note_node_disk(c, "render", "render", free_gb=400.0, total_gb=500.0, now=NOW)
    c.commit()
    os.environ["VS_ALERT_DISK_GB"] = "100"
    import importlib
    importlib.reload(A)
    got = sorted(A.detect(c, NOW))
    check("тревога про машину, где мало", got, ["disk_low_node:minsk"])
    check("и сказано, какая это машина",
          "minsk" in A.detect(c, NOW)["disk_low_node:minsk"].title, True)
    check("сказано, что это симптом, а не просто полный диск",
          "мусор остаётся" in A.detect(c, NOW)["disk_low_node:minsk"].money, True)

    print("\n  молчащая машина не кричит вечно")
    note_node_disk(c, "minsk", "upload", free_gb=3.0, total_gb=500.0,
                   now=NOW - 2 * 3600)
    c.commit()
    check("сведения протухли — тревоги нет", sorted(A.detect(c, NOW)), [])
    os.environ["VS_ALERT_DISK_GB"] = "0"
    importlib.reload(A)

    print("\nПодача: одно сообщение на случай")
    c = fresh()
    c.execute("INSERT INTO queue_tasks VALUES(1,'dev','images','running',?,NULL)",
              (NOW - 2 * HOUR,))
    bag = Bag()
    A.tick(c, bag, now=NOW)
    check("сказали один раз", len(bag.sent), 1)
    A.tick(c, bag, now=NOW + 5 * 60)
    A.tick(c, bag, now=NOW + 30 * 60)
    check("через полчаса молчим", len(bag.sent), 1)
    A.tick(c, bag, now=NOW + HOUR + 60)
    check("через час повторили", len(bag.sent), 2)
    check("повтор помечен как повтор", bag.last().startswith("ВСЁ ЕЩЁ"), True)

    print("\nПодача: «починилось» обязательно")
    c.execute("UPDATE queue_tasks SET status='done', finished=?", (NOW + HOUR + 120,))
    A.tick(c, bag, now=NOW + HOUR + 180)
    check("сказали, что починилось", bag.last().startswith("ПОЧИНИЛОСЬ"), True)
    n = len(bag.sent)
    A.tick(c, bag, now=NOW + 2 * HOUR)
    check("и только один раз", len(bag.sent), n)

    print("\nПодача: молчащий телеграм не съедает тревогу")
    c = fresh()
    c.execute("INSERT INTO queue_tasks VALUES(1,'dev','images','running',?,NULL)",
              (NOW - 2 * HOUR,))
    dead, alive = Bag(ok=False), Bag()
    A.tick(c, dead, now=NOW)
    check("неотправленное не считается сказанным",
          (A.load_state(c).get("queue_stalled") or {}).get("sent"), 0.0)
    A.tick(c, alive, now=NOW + 60)
    check("следующий проход говорит заново", len(alive.sent), 1)
    check("и это по-прежнему первая тревога, а не повтор",
          alive.last().startswith("ТРЕВОГА"), True)

    print("\n  «починилось» тоже не теряется при отказе транспорта")
    c.execute("UPDATE queue_tasks SET status='done', finished=?", (NOW + 120,))
    A.tick(c, dead, now=NOW + 180)
    check("обязанность осталась в памяти", "queue_stalled" in A.load_state(c), True)
    A.tick(c, alive, now=NOW + 240)
    check("сказали, когда связь вернулась", alive.last().startswith("ПОЧИНИЛОСЬ"), True)
    check("память очищена", A.load_state(c), {})

    print("\nПодача: разные провайдеры — разные сообщения")
    c = fresh()
    c.execute("INSERT INTO admin_state VALUES('credential_cooldown','%s',0)" % (
        '{"a:1":{"family":"googler","mask":"m1","until":%d,"why":"429"},'
        '"b:1":{"family":"elevenlabs","mask":"m2","until":%d,"why":"402"}}'
        % (int(NOW + 4 * HOUR), int(NOW + 4 * HOUR))))
    bag = Bag()
    A.tick(c, bag, now=NOW)
    check("два ключа — два сообщения", len(bag.sent), 2)

    print("\nПамять переживает перезапуск")
    c = fresh()
    c.execute("INSERT INTO queue_tasks VALUES(1,'dev','images','running',?,NULL)",
              (NOW - 2 * HOUR,))
    bag = Bag()
    A.tick(c, bag, now=NOW)
    state = A.load_state(c)
    check("состояние записано в admin_state", "queue_stalled" in state, True)
    A.save_state(c, state)                       # как будто процесс перезапустился
    A.tick(c, bag, now=NOW + 10 * 60)
    check("после перезапуска не кричим заново", len(bag.sent), 1)

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
