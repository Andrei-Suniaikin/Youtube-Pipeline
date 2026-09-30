#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Массовые действия над отмеченными каналами — сервер (владелец 30.09.2026).

«Можно сделать как в айфоне в галерее — кнопку "Выбрать", и потом уже выбор действия с
этими карточками». Раньше массовых действий не было вовсе: поставить на паузу десять
каналов — десять заходов в карточку.

Что здесь проверяется и почему именно это:

  • НЕ «ВСЁ ИЛИ НИЧЕГО». Отказ по одному каналу не отменяет остальные, и в ответе
    сказано поимённо, у кого не вышло и почему. Общая транзакция откатила бы уже
    сделанное, и человек, увидев одну ошибку, не знал бы, что из двадцати каналов
    изменилось, а что нет.
  • «МЕНЯТЬ БЫЛО НЕЧЕГО» ≠ «СДЕЛАНО». Канал, который уже стоял на паузе, не прибавляет
    к сделанному: выдавать его за проделанную работу — врать о том, что произошло.
  • УДАЛЕНИЯ В МАССОВЫХ ДЕЙСТВИЯХ НЕТ И НЕ БУДЕТ. Удаление необратимо и не проходит у
    канала с историей; пачкой это однажды снесёт не то. Безопасный шаг — архив.
  • ТЕГ ДОПИСЫВАЕТСЯ, А НЕ ПЕРЕПИСЫВАЕТ НАБОР. Массовое «проставить тег» через
    `set_tags` стёрло бы всё, что на канале уже висит.
  • ЧУЖОЙ КАНАЛ НЕ ТРОГАЕМ. Номер канала приходит с экрана, и маршрут обязан отвечать
    на него так же, как на несуществующий.

    .venv/bin/python tests/test_channels_bulk.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="channels-bulk-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "web"))

from pub_schema import ensure_pub_schema                           # noqa: E402

_c = sqlite3.connect(DB)
ensure_pub_schema(_c)
_c.executescript("""
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE,
    token TEXT, role TEXT, created TEXT, password_hash TEXT, daily_quota INTEGER,
    tenant_id TEXT, disabled INTEGER DEFAULT 0, keys TEXT);
  CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, user_id INTEGER, expires REAL);
  CREATE TABLE IF NOT EXISTS tenants(id TEXT PRIMARY KEY, name TEXT, created TEXT,
    is_active INTEGER, daily_quota INTEGER);
  CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT);
""")
_c.commit()
_c.close()

import app as W                                                    # noqa: E402
from fastapi import HTTPException                                  # noqa: E402

FAILS = []
TENANT = "t1"


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def refused(label, fn, code, part=""):
    try:
        fn()
        check(label, "прошло без отказа", f"отказ {code}")
    except HTTPException as exc:
        check(label, (exc.status_code, part in str(exc.detail)), (code, True))


def sql(q, args=()):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(q, args)]
    finally:
        c.close()


def run(q, args=()):
    c = sqlite3.connect(DB)
    try:
        c.execute(q, args)
        c.commit()
    finally:
        c.close()


def channel(name, status="active", tenant=TENANT):
    run("INSERT INTO pub_channels(tenant_id,name,uc,lang,status,tz,schedule,"
        "horizon_days,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (tenant, name, "UC" + name.ljust(22, "x")[:22], "pl", status,
         "Europe/Warsaw", "{}", 14, time.time(), time.time()))
    return sql("SELECT id FROM pub_channels WHERE name=? AND tenant_id=?",
               (name, tenant))[0]["id"]


def status_of(cid):
    rows = sql("SELECT status FROM pub_channels WHERE id=?", (cid,))
    return rows[0]["status"] if rows else None


def tags_of(cid):
    return [r["name"] for r in sql(
        "SELECT t.name FROM pub_tags t JOIN pub_channel_tags ct ON ct.tag_id=t.id "
        "WHERE ct.channel_id=? ORDER BY t.name", (cid,))]


def preset_of(cid):
    return sql("SELECT preset_channel_id p FROM pub_channels WHERE id=?", (cid,))[0]["p"]


def bulk(ids, action, **kw):
    return W.ui_channels_bulk(W.ChannelsBulkIn(ids=ids, action=action, **kw))


def bulk_ok(ids, action, **kw):
    """Как `bulk`, но отказ всему запросу превращает в понятный провал проверки.

    «Всё или ничего» — ровно та поломка, которую этот тест обязан назвать словами, а не
    уронить себя трассировкой: иначе непонятно, что именно сломалось.
    """
    try:
        return bulk(ids, action, **kw)
    except HTTPException as exc:
        return {"ok": False, "action": "запрос отклонён целиком", "asked": len(ids),
                "done": [], "same": [],
                "failed": [{"id": 0, "name": "запрос отклонён целиком",
                            "why": str(exc.detail)}]}


def main() -> int:
    print("Пауза пачкой: часть сделана, часть уже была, часть не вышла")
    a = channel("Альфа")
    b = channel("Бета")
    c_paused = channel("Гамма", status="paused")
    alien = channel("Чужой", tenant="t2")
    out = bulk_ok([a, b, c_paused, alien, 90001], "paused")
    check("сделано — только те, кого правда изменили",
          sorted(out["done"]), ["Альфа", "Бета"])
    check("уже стоявший на паузе назван отдельно", out["same"], ["Гамма"])
    check("действие названо по-человечески", out["action"], "поставить на паузу")
    check("не вышло — поимённо и с причиной",
          [(f["id"], f["why"]) for f in out["failed"]],
          [(alien, "канала нет — возможно, его уже удалили"),
           (90001, "канала нет — возможно, его уже удалили")])
    check("у неудачи есть имя для экрана",
          [f["name"] for f in out["failed"]], [f"канал №{alien}", "канал №90001"])
    check("сказано, сколько просили", out["asked"], 5)
    # Главное: отказ по двум номерам не отменил двух удавшихся.
    check("Альфа на паузе", status_of(a), "paused")
    check("Бета на паузе", status_of(b), "paused")
    check("чужой канал не тронут", status_of(alien), "active")

    print("Запись сорвалась на полпути — сделанное остаётся сделанным")
    d = channel("Дельта")
    e = channel("Эпсилон")
    родной = W.pub_ui.set_channel_status

    def капризный(c, tenant_id, cid, status, now=None):
        # Подменяем зависимость, а не правим код: единственная достижимая сегодня причина
        # отказа по одному каналу — «канала нет», а сорваться запись может и на занятой
        # базе. Проверяем именно то, что тогда произойдёт с уже записанным.
        if cid == e:
            raise W.pub_ui.ChannelError("база занята другой записью, повторите")
        return родной(c, tenant_id, cid, status, now=now)

    W.pub_ui.set_channel_status = капризный
    try:
        out = bulk_ok([d, e], "paused")
    finally:
        W.pub_ui.set_channel_status = родной
    check("что успели — сделано", out["done"], ["Дельта"])
    check("и это в базе, а не откатилось вместе с отказом", status_of(d), "paused")
    check("отказ назван поимённо и с причиной",
          [(f["name"], f["why"]) for f in out["failed"]],
          [("Эпсилон", "база занята другой записью, повторите")])
    check("сорвавшийся канал не изменился", status_of(e), "active")

    print("Возврат в работу и архив — тем же списком")
    out = bulk([a, b], "active")
    check("оба вернулись", sorted(out["done"]), ["Альфа", "Бета"])
    check("Альфа работает", status_of(a), "active")
    out = bulk([a, b], "archived")
    check("оба в архиве", sorted(out["done"]), ["Альфа", "Бета"])
    check("Бета в архиве", status_of(b), "archived")
    check("повтор ничего не делает и так и говорит",
          bulk([a, b], "archived")["same"], ["Альфа", "Бета"])
    check("и сделанного в повторе нет", bulk([a], "archived")["done"], [])

    print("Тег дописывается, а не переписывает набор")
    run("INSERT INTO pub_tags(tenant_id,name,created) VALUES(?,?,?)", (TENANT, "ww2", 1))
    run("INSERT INTO pub_channel_tags(channel_id,tag_id) SELECT ?,id FROM pub_tags "
        "WHERE name='ww2'", (a,))
    out = bulk([a, b], "tag_add", tag="  НовыЕ30 ")
    check("проставлен обоим", sorted(out["done"]), ["Альфа", "Бета"])
    check("старый тег на месте", sorted(tags_of(a)), ["ww2", "новые30"])
    check("имя тега приведено к одному виду", tags_of(b), ["новые30"])
    check("повтор — менять было нечего",
          bulk([a], "tag_add", tag="новые30")["same"], ["Альфа"])
    out = bulk([a, b], "tag_remove", tag="новые30")
    check("снят у обоих", sorted(out["done"]), ["Альфа", "Бета"])
    check("снялся только он", tags_of(a), ["ww2"])
    check("снимать нечего — так и сказано",
          bulk([b], "tag_remove", tag="новые30")["same"], ["Бета"])
    refused("тег не задан — отказ всему запросу",
            lambda: bulk([a], "tag_add", tag="   "), 400, "не задан тег")
    check("и ничего не проставилось", tags_of(a), ["ww2"])

    print("Пресет канала")
    run("INSERT INTO pub_channel_presets(tenant_id,name,created,updated) VALUES(?,?,?,?)",
        (TENANT, "Ночной", time.time(), time.time()))
    pid = sql("SELECT id FROM pub_channel_presets WHERE name='Ночной'")[0]["id"]
    refused("несуществующий пресет — отказ до цикла",
            lambda: bulk([a, b], "channel_preset", preset_id=90002), 404, "пресет")
    check("и ни один канал на него не сел", (preset_of(a), preset_of(b)), (None, None))
    out = bulk([a, b], "channel_preset", preset_id=pid)
    check("посажены оба", sorted(out["done"]), ["Альфа", "Бета"])
    check("в базе стоит он", (preset_of(a), preset_of(b)), (pid, pid))
    check("повтор — менять было нечего",
          bulk([a], "channel_preset", preset_id=pid)["same"], ["Альфа"])
    out = bulk([a], "channel_preset", preset_id=None)
    check("снятие с пресета — это тоже работа", out["done"], ["Альфа"])
    check("и оно записано", preset_of(a), None)

    print("Чего в массовых действиях нет")
    for bad in ("delete", "удалить", "remove", "drop", "problem", ""):
        refused(f"действие «{bad}» не принимается",
                lambda bad=bad: bulk([a], bad), 400, "")
    check("удаления нет в самом списке действий",
          [k for k in W.BULK_ACTIONS if "del" in k or "удал" in k], [])
    check("канал на месте после всех попыток", status_of(a) is not None, True)
    check("список действий — ровно шесть известных",
          sorted(W.BULK_ACTIONS), ["active", "archived", "channel_preset",
                                   "paused", "tag_add", "tag_remove"])

    print("Границы запроса")
    refused("ни один канал не отмечен", lambda: bulk([], "paused"), 400, "не отмечен")
    refused("слишком много за раз",
            lambda: bulk(list(range(1, W.BULK_LIMIT + 2)), "paused"), 400,
            f"не больше {W.BULK_LIMIT}")
    # Повторы одного номера — обычное дело при клике по одной карточке дважды: канал
    # должен обработаться один раз, а не столько, сколько его прислали.
    out = bulk([b, b, b], "active")
    check("повторы номера свёрнуты", (out["asked"], out["done"]), (1, ["Бета"]))

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
