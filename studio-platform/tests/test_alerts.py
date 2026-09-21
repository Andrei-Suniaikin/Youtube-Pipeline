#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тревоги: одиннадцать поводов и три правила подачи.

Сети здесь нет вообще: `pub_alerts` отправку не делает, а транспорт подменён списком.
Поэтому тест гоняется где угодно, включая машину без телеграма и без интернета.

    .venv/bin/python tests/test_alerts.py
"""
from __future__ import annotations

import json
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
                               status TEXT, created REAL, finished REAL,
                               project_id TEXT, error TEXT);
      CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT);
      CREATE TABLE pub_upload_jobs(id INTEGER PRIMARY KEY, tenant_id TEXT, status TEXT,
                                   created REAL, finished REAL);
      CREATE TABLE pub_releases(id INTEGER PRIMARY KEY, tenant_id TEXT, plan_item_id INT,
                                slot_at REAL, phase TEXT, status TEXT);
      CREATE TABLE pub_plan_items(id INTEGER PRIMARY KEY, tenant_id TEXT, slot_at REAL,
                                  status TEXT);
    """)
    # Журнал платных вызовов заводим НЕ своей копией схемы: §7 спрашивает у него,
    # потрачены ли деньги, и расхождение с боевой таблицей тест бы не поймал.
    from pub_calls import ensure_calls_schema
    ensure_calls_schema(c)
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
    c.execute("INSERT INTO queue_tasks(job_id,tenant_id,kind,status,created,finished) VALUES(1,'dev','images','running',?,NULL)",
              (NOW - 10 * 60,))
    check("свежая задача поводом не является", keys(c), [])
    c.execute("UPDATE queue_tasks SET created=?", (NOW - 40 * 60,))
    check("ждёт дольше порога и ничего не завершалось", keys(c), ["queue_stalled"])
    c.execute("INSERT INTO queue_tasks(job_id,tenant_id,kind,status,created,finished) VALUES(2,'dev','tts','done',?,?)",
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
    c.execute("INSERT INTO queue_tasks(job_id,tenant_id,kind,status,created,finished) VALUES(1,'dev','images','queued',?,NULL)",
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
    c.execute("INSERT INTO queue_tasks(job_id,tenant_id,kind,status,created,finished) VALUES(1,'dev','images','running',?,NULL)",
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
    c.execute("INSERT INTO queue_tasks(job_id,tenant_id,kind,status,created,finished) VALUES(1,'dev','images','running',?,NULL)",
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

    print("\nПовод 8 — ключ исчерпан")
    c = fresh()
    def cooldown(why, family="openai", mask="sk-p…9f2a"):
        rec = {f"{family}:1": {"family": family, "mask": mask,
                               "until": NOW + 10 * 60, "why": why}}
        c.execute("INSERT OR REPLACE INTO admin_state VALUES('credential_cooldown',?,0)",
                  (json.dumps(rec, ensure_ascii=False),))
    cooldown("HTTP 429 — часовой лимит картинок", family="googler", mask="fg_l…b3gt")
    check("троттлинг ключом исчерпанным не считается",
          [k for k in keys(c) if k.startswith("key_exhausted")], [])
    cooldown("insufficient_quota: You exceeded your current quota")
    # §2 про этот же ключ молчит: одна беда — одно сообщение, и говорит о ней тот
    # повод, который знает причину.
    check("отказ про деньги — повод, и только он", keys(c), ["key_exhausted:openai"])
    bag = Bag()
    A.tick(c, bag, now=NOW)
    # §2 ждёт час, прежде чем сказать. Исчерпанный ключ сам не пройдёт — ждать нечего.
    check("сказано сразу, без выдержки", len(bag.sent), 1)
    check("в тексте — слова провайдера", "insufficient_quota" in bag.sent[0], True)
    check("и что это про деньги", "означает деньги" in bag.sent[0], True)
    c.execute("UPDATE admin_state SET v='{}' WHERE k='credential_cooldown'")
    A.tick(c, bag, now=NOW + 5 * 60)
    check("ключ заменили — сказали «починилось»",
          any("ПОЧИНИЛОСЬ" in m for m in bag.sent), True)

    print("\nПовод 9 — ключ FastGen истекает")
    c = fresh()

    def fg_snapshot(checked, expires, limit=6000, used=0):
        # Та же форма JSON, что кладёт main.save_fastgen_usage — тесты не знают о
        # кабинете, но обязаны читать ровно то, что он пишет.
        snap = {"checked": checked, "expires": expires, "window": "per_hour",
                "limits": {"img_gen_per_hour_limit": limit},
                "hourly": {"image_generation": used}, "threads": {}}
        c.execute("INSERT OR REPLACE INTO admin_state VALUES('fastgen_usage',?,0)",
                  (json.dumps(snap, ensure_ascii=False),))

    check("снимка ещё нет — повода нет", keys(c), [])
    fg_snapshot(NOW, NOW + 30 * 24 * HOUR)
    check("до истечения далеко — повода нет", keys(c), [])
    fg_snapshot(NOW, NOW + 3 * 24 * HOUR)
    check("осталось меньше порога (7 дней) — повод есть", keys(c), ["fastgen_key_expiring"])
    check("в тексте — дата и остаток",
          "осталось" in A.detect(c, NOW)["fastgen_key_expiring"].text, True)
    fg_snapshot(NOW, NOW - HOUR)
    # Срок прошёл — новость стала другой, и несёт её §11. Оставлять её внутри §9 было
    # нельзя дважды: «истекает» про мёртвый ключ читают как «время ещё есть», а дедуп
    # §9 к этому часу уже «сказан» и новой тревоги не поднял бы вовсе.
    check("ключ уже истёк — §9 молчит, говорит §11", keys(c), ["key_dead:googler"])
    fg_snapshot(NOW, NOW + 30 * 24 * HOUR)
    check("ключ продлили — повод снят", keys(c), [])

    print("\n  устаревший снимок — не повод (ни про срок, ни про расход)")
    fg_snapshot(NOW - 3 * HOUR, NOW + 3 * 24 * HOUR, limit=6000, used=6000)
    check("снимок старше двух часов — оба повода молчат", keys(c), [])

    print("\n  review issue 1 — тревога прозвучала → снимок протух → «ПОЧИНИЛОСЬ»"
          " ждёт свежих данных, а не сыплется по тишине сети")
    c = fresh()
    fg_snapshot(NOW, NOW + 3 * 24 * HOUR, limit=6000, used=5900)  # оба повода верны
    check("сразу оба повода верны", sorted(keys(c)),
          ["fastgen_key_expiring", "fastgen_usage_near_limit"])
    bag = Bag()
    A.tick(c, bag, now=NOW)
    check("обе тревоги прозвучали", len(bag.sent), 2)
    # снимок не обновлялся два часа с лишним — сеть недоступна, ключ НЕ продлён,
    # расход НЕ упал, просто цифр больше нет.
    A.tick(c, bag, now=NOW + 2 * HOUR + 60)
    check("устаревший снимок — «ПОЧИНИЛОСЬ» НЕ отправлено (было отправлено до фикса)",
          any("ПОЧИНИЛОСЬ" in m for m in bag.sent), False)
    check("и вообще ничего нового не отправлено, пока данных нет", len(bag.sent), 2)
    # свежий снимок: ключ продлили И расход упал — вот теперь «починилось» законно
    fg_snapshot(NOW + 2 * HOUR + 60, NOW + 30 * 24 * HOUR, limit=6000, used=100)
    A.tick(c, bag, now=NOW + 2 * HOUR + 120)
    fixed = [m for m in bag.sent if "ПОЧИНИЛОСЬ" in m]
    check("свежие данные подтвердили починку — два «ПОЧИНИЛОСЬ», по каждому поводу",
          len(fixed), 2)

    print("\nПовод 10 — расход картинок близок к часовому лимиту")
    c = fresh()
    fg_snapshot(NOW, NOW + 30 * 24 * HOUR, limit=6000, used=5000)
    check("83% — ниже порога, повода нет", keys(c), [])
    fg_snapshot(NOW, NOW + 30 * 24 * HOUR, limit=6000, used=5400)
    check("90% — повод есть", keys(c), ["fastgen_usage_near_limit"])
    check("в тексте — сколько из скольки за час",
          "5400 из 6000" in A.detect(c, NOW)["fastgen_usage_near_limit"].text, True)

    print("\n  повод 10 повторяется по общему правилу и снимается «починилось»")
    bag = Bag()
    A.tick(c, bag, now=NOW)
    check("сказали", len(bag.sent), 1)
    A.tick(c, bag, now=NOW + 30 * 60)
    check("через полчаса молчим", len(bag.sent), 1)
    A.tick(c, bag, now=NOW + HOUR + 60)
    check("через час повторили", len(bag.sent), 2)
    fg_snapshot(NOW + HOUR + 60, NOW + 30 * 24 * HOUR, limit=6000, used=1000)
    A.tick(c, bag, now=NOW + HOUR + 120)
    check("расход упал — сказали «починилось»",
          any("ПОЧИНИЛОСЬ" in m for m in bag.sent), True)

    print("\nПовод 11 — ключ кончился, нужен новый")
    # Зачем повод вообще: §8 «ключ исчерпан» читает `admin_state.credential_cooldown`,
    # а туда пишет только канал промптов (`main._PROMPT_CHANNEL_FAMILY`). Ключ картинок
    # и обложек в эту запись не попадает, поэтому про него §8 не срабатывал ни разу —
    # мёртвый ключ доходил до телеграма одним §7 «задача упала»: без повтора, без
    # «починилось» и без слова «ключ» в заголовке. Заказчик ставит новый ключ сам, и
    # ему нужно ровно одно сообщение: этот ключ кончился, поставьте новый.
    c = fresh()

    def refused(times=1, code=401, at=NOW, family="googler"):
        """Как будто провайдер ОТВЕТИЛ и ключ не принял: то, что в бою пишет
        `dev/alerts.py:refresh_fastgen_usage`. Сети здесь по-прежнему нет."""
        for i in range(times):
            A.note_key_refused(c, family, code, at + i, "fg_l…b3gt")

    check("отказов не было — повода нет", keys(c), [])
    refused(1)
    # Статистику спрашиваем раз в минуту, и 401 приходит в том числе на сбое у самого
    # провайдера. Поднимать по одному ответу — ложная тревога раз в час.
    check("один отказ — ещё не повод", keys(c), [])
    refused(1, at=NOW + 60)
    check("второй отказ подряд — повод", keys(c, NOW + 60), ["key_dead:googler"])
    a = A.detect(c, NOW + 60)["key_dead:googler"]
    check("названо семейство ключа", "googler" in a.title, True)
    check("сказано, что нужен новый", "Нужен новый ключ" in a.text, True)
    check("сказано, что именно случилось", "HTTP 401" in a.text, True)
    check("сказано, что делать", "положить его в файл ключей" in a.text, True)
    check("значение ключа не печатается — только маска", "fg_live" in a.text, False)
    check("деньги названы: проверка бесплатна", "0 кредитов" in a.money, True)

    print("\n  подаётся как состояние: раз в час, и «починилось» по факту замены")
    bag = Bag()
    A.tick(c, bag, now=NOW + 60)
    check("сказали сразу — ждать тут нечего", len(bag.sent), 1)
    A.tick(c, bag, now=NOW + 30 * 60)
    check("через полчаса молчим", len(bag.sent), 1)
    refused(1, at=NOW + 30 * 60)          # отказ продолжается, его подтверждают
    A.tick(c, bag, now=NOW + HOUR + 120)
    check("через час повторили: «всё ещё не заменили» — новость", len(bag.sent), 2)
    # В бою принятый запрос делает две вещи разом: снимает заявку на отказ и кладёт
    # свежий снимок. Второе обязательно — без свежих цифр «починилось» придержано
    # (FASTGEN_STATE_KEYS), иначе обрыв связи выглядел бы как замена ключа.
    A.note_key_accepted(c, "googler", NOW + HOUR + 180)
    fg_snapshot(NOW + HOUR + 180, NOW + 30 * 24 * HOUR)
    A.tick(c, bag, now=NOW + HOUR + 180)
    check("новый ключ приняли — сказали «починилось»",
          any("ПОЧИНИЛОСЬ" in m for m in bag.sent), True)

    print("\n  вышедший срок — тот же повод, и он ОДИН, а не два")
    c = fresh()
    fg_snapshot(NOW, NOW - HOUR)
    a = A.detect(c, NOW)["key_dead:googler"]
    check("срок в прошлом — повод есть и без отказа", keys(c), ["key_dead:googler"])
    check("названа дата, до которой ключ действовал",
          time.strftime("%d.%m.%Y", time.localtime(NOW - HOUR)) in a.text, True)
    refused(2)
    check("отказ и вышедший срок вместе — всё равно один повод про один ключ",
          keys(c, NOW + 60), ["key_dead:googler"])

    print("\n  §9 не «чинится», когда перерастает в §11")
    # Оба проверяющих воспроизвели это на живом тике: в момент истечения §9 уходит из
    # активных, §11 появляется — и сторож слал «ПОЧИНИЛОСЬ · Ключ FastGen истекает»
    # ВМЕСТЕ с «ТРЕВОГА · Ключ кончился». Заказчик получал «починилось» ровно тогда,
    # когда всё сломалось; хуже сообщения придумать трудно.
    c = fresh()
    sent = []
    fg_snapshot(NOW, NOW + 48 * HOUR)                 # истекает через два дня → §9
    A.tick(c, lambda t: (sent.append(t), True)[1], now=NOW)
    check("сначала прозвучало «истекает»",
          any("истекает" in t and "ТРЕВОГА" in t for t in sent), True)
    sent.clear()
    fg_snapshot(NOW + 48 * HOUR, NOW + 48 * HOUR - 60)  # срок вышел, снимок свежий → §11
    A.tick(c, lambda t: (sent.append(t), True)[1], now=NOW + 48 * HOUR)
    check("теперь звучит «кончился»", any("кончился" in t for t in sent), True)
    check("и НИКАКОГО «починилось» про истечение",
          any("ПОЧИНИЛОСЬ" in t for t in sent), False)

    print("\n  протухшие данные не чинят мёртвый ключ (то же правило, что у §9/§10)")
    c = fresh()
    fg_snapshot(NOW, NOW - HOUR)
    bag = Bag()
    A.tick(c, bag, now=NOW)
    check("тревога прозвучала", len(bag.sent), 1)
    A.tick(c, bag, now=NOW + 3 * HOUR)
    check("снимок протух — «ПОЧИНИЛОСЬ» не отправлено",
          any("ПОЧИНИЛОСЬ" in m for m in bag.sent), False)
    fg_snapshot(NOW + 3 * HOUR, NOW + 30 * 24 * HOUR)
    A.tick(c, bag, now=NOW + 3 * HOUR + 60)
    check("свежий снимок с новым сроком — вот теперь «починилось»",
          any("ПОЧИНИЛОСЬ" in m for m in bag.sent), True)

    print("\n  отказ перестали подтверждать — молчим в обе стороны")
    # Так бывает, когда мёртвый ключ просто убрали из файла: спрашивать стало нечем,
    # подтверждений больше нет. Это не значит, что ключ заменили.
    c = fresh()
    refused(2)
    bag = Bag()
    A.tick(c, bag, now=NOW + 60)
    check("сказали", len(bag.sent), 1)
    check("третий час без подтверждений — повод больше не утверждаем",
          keys(c, NOW + 3 * HOUR), [])
    A.tick(c, bag, now=NOW + 3 * HOUR)
    check("но и «ПОЧИНИЛОСЬ» не сочинили — свежих цифр нет",
          any("ПОЧИНИЛОСЬ" in m for m in bag.sent), False)

    print("\nПовод 7 — задача упала")
    c = fresh()
    c.execute("INSERT INTO projects VALUES('p_1','Рассвет в горах')")
    def failed(job_id, kind, when, error="rc=2"):
        c.execute("INSERT INTO queue_tasks(job_id,tenant_id,kind,status,created,"
                  "finished,project_id,error) VALUES(?,'dev',?,'error',?,?,'p_1',?)",
                  (job_id, kind, when - 5, when, error))
    check("падений нет — повода нет", keys(c), [])
    failed(1, "prompts", NOW - 10 * 60)
    check("свежее падение — повод сразу, без выдержки", keys(c), ["failed:1"])
    # С тенантом ходит экран тревог. Раньше запрос падал здесь и нигде больше.
    check("с фильтром по тенанту тоже",
          sorted(A.detect(c, NOW, "dev").keys()), ["failed:1"])
    check("чужой тенант не видит", sorted(A.detect(c, NOW, "чужой").keys()), [])
    c.execute("UPDATE queue_tasks SET finished=? WHERE job_id=1", (NOW - 30 * HOUR,))
    c.execute("UPDATE queue_tasks SET created=? WHERE job_id=1", (NOW - 30 * HOUR,))
    check("старое падение из окна вышло", keys(c), [])

    print("\nПро падение говорят один раз")
    c = fresh()
    c.execute("INSERT INTO projects VALUES('p_1','Рассвет в горах')")
    failed(1, "prompts", NOW - 10 * 60, "OpenAI-ключ невалиден (HTTP 401)")
    bag = Bag()
    A.tick(c, bag, now=NOW)
    check("сказали сразу", len(bag.sent), 1)
    check("в тексте есть причина", "HTTP 401" in bag.sent[0], True)
    check("и название проекта", "Рассвет в горах" in bag.sent[0], True)
    # Остальные поводы через час повторяются: беда растёт. У падения ничего не растёт.
    A.tick(c, bag, now=NOW + 3 * HOUR)
    check("через три часа не повторили", len(bag.sent), 1)
    # Повтор задачи убирает её из повода — но это не «починилось»: так же выглядит
    # отмена и старение. Обещать, что стало хорошо, мы не вправе.
    c.execute("UPDATE queue_tasks SET status='cancelled' WHERE job_id=1")
    A.tick(c, bag, now=NOW + 4 * HOUR)
    check("«починилось» не отправлено", [m for m in bag.sent if "ПОЧИНИЛОСЬ" in m], [])
    check("и вообще ничего нового", len(bag.sent), 1)

    print("\nПачка падений не заваливает канал")
    c = fresh()
    c.execute("INSERT INTO projects VALUES('p_1','Рассвет в горах')")
    for i in range(1, 13):
        failed(i, "images", NOW - i * 60)
    ks = keys(c)
    check("поимённо не больше потолка", len([k for k in ks if k.startswith("failed:")
                                             and k != "failed:ещё"]), A.FAILED_MAX)
    check("и одна строка про остальные", "failed:ещё" in ks, True)
    bag = Bag()
    A.tick(c, bag, now=NOW)
    check("сообщений столько же", len(bag.sent), A.FAILED_MAX + 1)
    check("названы самые свежие", "#1 " in " ".join(bag.sent), True)
    check("а самое старое — нет", "#12 " in " ".join(bag.sent), False)

    print("\nДеньги названы прямо")
    c = fresh()
    c.execute("INSERT INTO projects VALUES('p_1','Рассвет в горах')")
    failed(1, "images", NOW - 60)
    bag = Bag(); A.tick(c, bag, now=NOW)
    check("без журнала — так и сказано",
          "Оплаченных вызовов по этому этапу не записано" in bag.sent[0], True)
    c = fresh()
    c.execute("INSERT INTO projects VALUES('p_1','Рассвет в горах')")
    failed(1, "images", NOW - 60)
    for i in range(3):
        c.execute("INSERT INTO provider_calls(tenant_id,project_id,kind,unit,fingerprint,"
                  "provider,status,cost,created,updated) "
                  "VALUES('dev','p_1','images',?,?,'googler','delivered',1,?,?)",
                  (str(i), f"fp{i}", NOW, NOW))
    bag = Bag(); A.tick(c, bag, now=NOW)
    check("с журналом — сколько уже оплачено",
          "До падения оплачено вызовов: 3" in bag.sent[0], True)

    print("\nПамять переживает перезапуск")
    c = fresh()
    c.execute("INSERT INTO queue_tasks(job_id,tenant_id,kind,status,created,finished) VALUES(1,'dev','images','running',?,NULL)",
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
