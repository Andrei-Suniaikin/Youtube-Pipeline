#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Три состояния выхода — три разных ответа, и таймер, который их подтверждает (ТЗ §11).

Требование ТЗ: «"Готов файл", "назначена дата" и "вышел публично" показывай отдельно».
Правила приёмки второго документа: «для наступивших сроков подтверждён фактический
выход». Беда, ради которой написан этот тест: механизм поздней проверки был написан и
оттестирован, но по расписанию не запускался, — значит третье состояние подтверждать
было некому, а экран всё равно называл вторым словом первое.

Что здесь проверяется и почему именно это.

  • ТРИ СОСТОЯНИЯ РАЗЛИЧИМЫ В ДАННЫХ, А НЕ ТОЛЬКО НА ЭКРАНЕ. Выпуск в работе,
    выпуск с подтверждённым расписанием и выпуск с подтверждённым выходом обязаны
    приходить в календарь разными состояниями. Пока «расписание подтверждено» и
    «ролик вышел» назывались одним словом `done`, весь месяц выглядел выпущенным
    ещё до того, как хоть один ролик проверили.
  • ПОДНИМАЕТ ФАЗУ ТОЛЬКО ФАКТ. `public` — да; «доступен по ссылке» — нет, это
    дефект; «ключ не видит» — нет, ждём. Повышение фазы задним числом необратимо
    врёт владельцу про сотню каналов сразу.
  • НЕТ КЛЮЧА — ЭТО ПРИЧИНА, А НЕ ПРИГОВОР. Без ключа YouTube проверка обязана
    сказать, чего ей не хватает, и не пометить ни одного выпуска «не вышел»:
    молчание API и невыход ролика — разные вещи, и путать их значит послать
    оператора чинить исправные каналы.
  • ЗАЛЕЖАВШИЙСЯ РОЛИК НЕ ЖЖЁТ КВОТУ. Помеченный к разбору выпуск остаётся в фазе
    `scheduled` навсегда, то есть из очереди проверки не уходит никогда. На
    получасовом шаге сотня таких съедала бы половину суточного кошелька YouTube,
    общего со статистикой и ночным обходом, — и обещание в комментарии таймера
    («до 18% кошелька») стало бы неправдой.
  • ТАЙМЕР ЗАПУСКАЕТ ИМЕННО ТО, ЧТО НАПИСАНО. ExecStart из юнита выполняется
    по-настоящему: перепутанный ключ (`--once` вместо `--run`) или переименованный
    сценарий иначе обнаружились бы только на боевой машине.

Сеть не трогается: ветки без ключа до неё не доходят, остальным транспорт передаётся
отдельным аргументом.

    .venv/bin/python tests/test_public_release_states.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "server"))

FAILS = []
HOUR = 3600.0


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


# ─────────────────────────── общая временная база ───────────────────────────

WORK = tempfile.mkdtemp(prefix="public-states-")
DB = os.path.join(WORK, "vsgen.db")
# Пустой `.env` и несуществующий файл ключей: настоящий ключ владельца не должен
# попасть в тест ни через `.env` проекта, ни через `~/video_studio_api_keys.json` —
# иначе ветка «ключа нет» тихо превратилась бы в поход в сеть.
EMPTY_ENV = os.path.join(WORK, "empty.env")
open(EMPTY_ENV, "w", encoding="utf-8").close()
NO_KEYS = os.path.join(WORK, "нет-такого-файла.json")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "dev"
os.environ["VS_ENV_FILE"] = EMPTY_ENV
os.environ["VS_SERVER_KEYS_PATH"] = NO_KEYS
os.environ["VS_TENANTS_DIR"] = os.path.join(WORK, "tenants")
os.environ.pop("VS_YT_API_KEY", None)


def conn():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def add_release(c, cid, slot_at, phase, status, video_id="vidPUB", title="Донор"):
    """Ссылка в плане + выпуск по ней. Возвращает release_id."""
    now = time.time()
    c.execute("INSERT INTO pub_plan_items(tenant_id,channel_id,version,donor_url,"
              "position,slot_at,donor_title,status,created,updated) "
              "VALUES('dev',?,1,?,?,?,?,'planned',?,?)",
              (cid, f"https://youtu.be/d{int(slot_at)}", slot_at, slot_at, title,
               now, now))
    item = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    rid = f"rel-{item}"
    c.execute("INSERT INTO pub_releases(release_id,tenant_id,plan_item_id,"
              "plan_item_version,channel_id,uc,slot_at,project_id,status,phase,"
              # project_id намеренно NULL: таблицы `projects` в этой базе нет, а
              # состояния слота от неё не зависят — тест не должен тащить за собой
              # половину схемы кабинета ради поля, которое здесь не проверяется.
              "video_id,created,updated) VALUES(?,'dev',?,1,?,'UC1',?,NULL,?,?,?,?,?)",
              (rid, item, cid, slot_at, status, phase, video_id, now, now))
    c.commit()
    return rid


def main() -> int:
    from pub_schema import ensure_pub_schema                          # noqa: PLC0415
    c0 = conn()
    ensure_pub_schema(c0)
    c0.commit()
    c0.close()

    import web.app as pult                                            # noqa: PLC0415
    import pub_ui as U                                                # noqa: PLC0415
    import pub_verify as V                                            # noqa: PLC0415

    now = time.time()

    # ── 1. Три состояния различимы в данных ────────────────────────────────
    print("Три состояния выпуска приходят разными состояниями слота")
    c = conn()
    cid = U.create_channel(c, "dev", "", "Канал", now=now - 40 * 24 * HOUR)
    c.execute("UPDATE pub_channels SET schedule=?, tz='UTC', horizon_days=3 WHERE id=?",
              (json.dumps({"*": ["09:00"]}), cid))
    c.commit()
    # Файл готов, работа идёт: заливки ещё не было.
    add_release(c, cid, now - 30 * HOUR, "cover_ready", "running", video_id="")
    # Назначена дата: Studio подтвердила расписание — и только это.
    add_release(c, cid, now - 20 * HOUR, "scheduled", "done", video_id="vidSCHED")
    # Вышел публично: поздняя проверка увидела ролик и подняла фазу.
    add_release(c, cid, now - 10 * HOUR, "published", "done", video_id="vidOUT")
    ch = dict(c.execute("SELECT * FROM pub_channels_live WHERE id=?", (cid,)).fetchone())
    cal = pult._calendar(c, ch, 3)
    c.close()

    by_rel = {r["release_id"]: r for r in cal if r["release_id"]}
    check("файл готов — состояние «в работе»", by_rel["rel-1"]["state"], "running")
    # Мутация-проверка: верни в `row_for` одну ветку по статусу (`{"done": "done", …}`)
    # — оба выпуска ниже снова станут `done`, и эти две строки поймают разницу первыми.
    check("назначена дата — состояние «done», а не «вышел»",
          by_rel["rel-2"]["state"], "done")
    check("подтверждён выход — отдельное состояние «public»",
          by_rel["rel-3"]["state"], "public")
    check("и это ТРИ разных слова, а не два",
          len({by_rel[k]["state"] for k in ("rel-1", "rel-2", "rel-3")}), 3)

    print("\n  счётчики карточки канала считают их врозь")
    d = asyncio.run(pult.ui_channel(cid, history=3))
    check("подтверждённых выходов — ровно один", d["counts"].get("public"), 1)
    check("назначенных, но не подтверждённых — ровно один", d["counts"].get("done"), 1)
    # Мутация-проверка: убери `"public": 0` из заготовки `by_day` — сетка месяца
    # перестанет отличать «подтверждённых выходов ноль» от «такого ключа нет вовсе»,
    # и эта строка поймает разницу.
    days = d["by_day"]
    check("в сетке месяца ключ «public» есть у КАЖДОГО дня, даже пустого",
          all("public" in box for box in days.values()), True)
    check("и подтверждённый выход попал ровно в один день",
          sum(box.get("public", 0) for box in days.values()), 1)

    # ── 2. Фазу поднимает только факт ──────────────────────────────────────
    print("\nФазу поднимает только подтверждённый публичный ролик")

    def one(privacy, slot_ago=3 * HOUR, phase="scheduled"):
        cc = conn()
        cc.execute("DELETE FROM pub_releases")
        cc.execute("DELETE FROM pub_plan_items")
        cc.execute("DELETE FROM pub_release_events")
        cc.execute("DELETE FROM pub_release_log")
        cc.commit()
        rid = add_release(cc, cid, now - slot_ago, phase, "done", video_id="vidX")
        body = ({"items": []} if privacy is None else
                {"items": [{"status": {"privacyStatus": privacy,
                                       "uploadStatus": "processed"},
                            "snippet": {"publishedAt": "2026-09-20T10:00:00Z"}}]})
        out = V.run(cc, "dev", now, fetch=lambda url: body, key="k")
        row = cc.execute("SELECT phase, blocked_reason FROM pub_releases "
                         "WHERE release_id=?", (rid,)).fetchone()
        cc.close()
        return out, row["phase"], row["blocked_reason"]

    out, phase, _ = one("public")
    check("публичный — фаза поднята до published", phase, "published")
    check("и посчитан как вышедший", out["published"], 1)
    out, phase, why = one("unlisted")
    check("только по ссылке — фаза НЕ поднята", phase, "scheduled")
    check("и это названо дефектом, а не выходом",
          "только по ссылке" in (why or ""), True)
    out, phase, why = one(None)
    check("ключ ролика не видит — фаза НЕ поднята", phase, "scheduled")
    check("и до истечения терпения никаких пометок", why, None)

    # ── 3. Нет ключа — причина, а не «не вышел» ────────────────────────────
    print("\nНет ключа YouTube — честная причина, а не вид «не вышел»")
    cc = conn()
    cc.execute("DELETE FROM pub_releases")
    cc.execute("DELETE FROM pub_plan_items")
    cc.execute("DELETE FROM pub_release_events")
    cc.execute("DELETE FROM pub_release_log")
    cc.commit()
    # Слот прошёл ДАВНО — терпение (GIVE_UP_SEC, 6 ч) заведомо вышло. Именно здесь
    # соблазн записать «не вышел» максимален, а вывод сделать не из чего.
    rid = add_release(cc, cid, now - 20 * HOUR, "scheduled", "done", video_id="vidY")
    out = V.run(cc, "dev", now, fetch=lambda url: {"items": []}, key="")
    row = cc.execute("SELECT phase, blocked_reason FROM pub_releases WHERE release_id=?",
                     (rid,)).fetchone()
    logged = cc.execute("SELECT COUNT(*) FROM pub_release_log").fetchone()[0]
    cc.close()
    check("ни один выпуск не объявлен непроверенным", out["stale"], 0)
    check("зато посчитан как «вывода нет»", out["unknown"], 1)
    check("фаза не тронута", row["phase"], "scheduled")
    # Мутация-проверка: убери ранний возврат `if not key` из `V.run` — выпуск получит
    # `blocked_reason` про невидимый ролик, и эти две строки поймают разницу первыми.
    check("и на выпуске нет пометки «выход не подтверждён»", row["blocked_reason"], None)
    check("в журнал выпуска тоже ничего не наврали", logged, 0)
    check("причина названа словами", "ключа YouTube Data API" in out["why"], True)
    check("и сказано, где его вставить", "Ключи" in out["why"], True)

    # ── 4. Залежавшийся не жжёт квоту ──────────────────────────────────────
    print("\nПомеченный к разбору спрашивается раз в сутки, а не раз в полчаса")
    cc = conn()
    cc.execute("DELETE FROM pub_releases")
    cc.execute("DELETE FROM pub_plan_items")
    cc.execute("DELETE FROM pub_release_events")
    cc.execute("DELETE FROM pub_release_log")
    cc.commit()
    # Слот прошёл ровно на терпение минус минута: первый вопрос ещё «до порога».
    slot = now - V.GIVE_UP_SEC + 60
    rid = add_release(cc, cid, slot, "scheduled", "done", video_id="vidZ")
    hits = {"n": 0}

    def silent(url):
        hits["n"] += 1
        return {"items": []}

    V.run(cc, "dev", now, fetch=silent, key="k")
    check("первый вопрос задан", hits["n"], 1)
    # Через терпение + шаг перепроверки: порог только что перейдён, прошлый вопрос был
    # ДО него — значит спрашиваем по обычному получасовому шагу и ставим пометку.
    t2 = slot + V.GIVE_UP_SEC + V.RECHECK_SEC
    V.run(cc, "dev", t2, fetch=silent, key="k")
    marked = cc.execute("SELECT blocked_reason FROM pub_releases WHERE release_id=?",
                        (rid,)).fetchone()[0]
    # Мутация-проверка: сделай `period` равным `STALE_RECHECK_SEC` по одному лишь
    # `now >= give_up_at` — пометка отложится на сутки, и эти две строки поймают
    # разницу первыми: вопрос не будет задан, пометки не появится.
    check("после порога вопрос задан сразу, а не через сутки", hits["n"], 2)
    check("и выпуск помечен к разбору", "ключ API ролика не видит" in (marked or ""), True)
    # Ещё полчаса спустя — уже помечен: суточный шаг, вопроса быть не должно.
    V.run(cc, "dev", t2 + V.RECHECK_SEC + 60, fetch=silent, key="k")
    check("через полчаса после пометки — не спрашиваем", hits["n"], 2)
    V.run(cc, "dev", t2 + V.STALE_RECHECK_SEC + 60, fetch=silent, key="k")
    check("через сутки — спрашиваем снова, ролик мог выйти позже срока", hits["n"], 3)
    cc.close()

    # ── 5. Таймер запускает ровно то, что написано ─────────────────────────
    print("\nТаймер: юниты на месте и ExecStart выполняется по-настоящему")
    svc = open(os.path.join(BASE, "deploy", "studio-verify.service"),
               encoding="utf-8").read()
    tmr = open(os.path.join(BASE, "deploy", "studio-verify.timer"),
               encoding="utf-8").read()
    check("служба — один проход и выход", "Type=oneshot" in svc, True)
    check("таймер зовёт именно эту службу", "Unit=studio-verify.service" in tmr, True)
    check("пропущенный запуск наверстывается", "Persistent=true" in tmr, True)
    # Мутация-проверка: замени `OnCalendar=` на `OnUnitActiveSec=` — эта строка поймает
    # разницу. Persistent без календарного расписания systemd молча игнорирует, и
    # обещание «наверстаем после включения машины» стало бы украшением.
    check("и наверстать есть чему: расписание календарное, иначе Persistent — пустышка",
          any(l.startswith("OnCalendar=") for l in tmr.splitlines()), True)

    exec_line = next(l for l in svc.splitlines() if l.startswith("ExecStart="))
    argv = shlex.split(exec_line.split("=", 1)[1])
    script = os.path.join(BASE, argv[1])
    check("сценарий из ExecStart существует", os.path.exists(script), True)

    env = dict(os.environ)
    env.update({"VS_DEV_DB": DB, "VS_ENV_FILE": EMPTY_ENV,
                "VS_SERVER_KEYS_PATH": NO_KEYS, "VS_DEV_TENANT": "dev"})
    env.pop("VS_YT_API_KEY", None)

    def run_cli():
        return subprocess.run([sys.executable, script, *argv[2:]], cwd=BASE, env=env,
                              capture_output=True, text=True, timeout=120)

    # Ждущих роликов нет: спрашивать было нечего, ключ и не требовался.
    cc = conn()
    cc.execute("DELETE FROM pub_releases")
    cc.execute("DELETE FROM pub_plan_items")
    cc.commit()
    cc.close()
    got = run_cli()
    # Мутация-проверка: замени `--run` в ExecStart на `--once` — argparse откажет,
    # и эта строка поймает разницу первой.
    check("пустой проход без ключа — не провал", got.returncode, 0)

    # Ролик ждёт, ключа нет: работа была, а сделать её было нечем.
    cc = conn()
    add_release(cc, cid, now - 20 * HOUR, "scheduled", "done", video_id="vidQ")
    cc.close()
    got = run_cli()
    # Мутация-проверка: верни безусловный `return 1` при пустом ключе — предыдущая
    # строка покраснеет; верни безусловный `return 0` — покраснеет эта.
    check("ждали ролики, а ключа нет — проход провален", got.returncode, 1)
    check("и сказано человеку, где взять ключ",
          "экране «Ключи»" in got.stdout, True)

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
