#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Машины заливки и профили AdsPower — ручки пульта (24.09.2026).

Браузерный тест подменяет сервер целиком, поэтому договор проверяется здесь: что
отдаётся на экран, что записывается, и — главное — чего НЕ должно случиться.

Самое важное: неудачная опись не трогает справочник ни строкой (пустой ответ машины
и обрыв связи — это сбой, а не «профилей больше нет»), а профиль, пропавший с машины,
остаётся видимым у канала, который на него настроен, иначе форма молча сохранила бы
«профиль не выбран» и канал остался бы без заливки.

    .venv/bin/python tests/test_hosts_profiles_api.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="hosts-api-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "web"))

from pub_schema import ensure_pub_schema                           # noqa: E402

conn = sqlite3.connect(DB)
ensure_pub_schema(conn)
conn.commit()
conn.close()

import app as W                                                    # noqa: E402
import pub_adspower                                                # noqa: E402
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


class FakeFetch:
    """То же, что отдаёт настоящая опись: профили и кто открыт прямо сейчас."""

    def __init__(self, profiles, active=(), pages=1):
        self.profiles = tuple(profiles)
        self.active_user_ids = frozenset(active)
        self.pages = pages


def rec(serial, title="", user_id="", group="", remark="", last_open=""):
    return pub_adspower.ProfileRecord(
        serial=serial, adspower_user_id=user_id or f"u{serial}", name=title,
        group_name=group, remark=remark, last_open_time=last_open)


def main() -> int:
    print("Машина заводится и правится")
    out = W.ui_host_add(W.HostIn(code="minsk", title="Минская машина",
                                 adspower_url="http://127.0.0.1:50325"))
    host_id = out["id"]
    check("заведена", (out["ok"], out["code"]), (True, "minsk"))
    refused("та же машина второй раз",
            lambda: W.ui_host_add(W.HostIn(code="minsk", adspower_url="http://x:1")),
            400, "уже заведена")
    refused("без короткого имени", lambda: W.ui_host_add(W.HostIn(code="  ")),
            400, "короткое имя")
    refused("адрес без http",
            lambda: W.ui_host_add(W.HostIn(code="mac", adspower_url="10.0.0.5:50325")),
            400, "http://")
    W.ui_host_save(host_id, W.HostIn(code="minsk", title="Минск, GPU",
                                     adspower_url="http://127.0.0.1:50325"))
    check("правка записана", sql("SELECT title FROM pub_hosts")[0]["title"], "Минск, GPU")
    refused("правка несуществующей", lambda: W.ui_host_save(777, W.HostIn(code="x")),
            404, "нет")

    print("«Убрать» не удаляет строку")
    W.ui_host_status(host_id, W.HostStatusIn(status="disabled"))
    rows = sql("SELECT id, status FROM pub_hosts")
    check("строка на месте", len(rows), 1)
    check("статус сменился", rows[0]["status"], "disabled")
    refused("выдуманный статус",
            lambda: W.ui_host_status(host_id, W.HostStatusIn(status="сломана")),
            400, "бывает")
    W.ui_host_status(host_id, W.HostStatusIn(status="active"))

    print("Опись машины")
    pub_adspower.fetch_host_profiles = lambda url, **kw: FakeFetch(
        [rec("46", "Профиль 46", "u46", group="history-pl"),
         rec("12", "Профиль 12", "u12")], active=["u46"])
    got = W.ui_host_refresh_profiles(host_id)
    check("сводка про найденное", (got["found"], got["new"]), (2, 2))
    check("сказано, кто открыт сейчас", got["active_now"], 1)
    profs = {p["serial"]: p for p in sql("SELECT * FROM pub_profiles")}
    check("оба профиля записаны", sorted(profs), ["12", "46"])
    check("название сохранено", profs["46"]["title"], "Профиль 46")
    check("открытый помечен", bool(profs["46"]["active_now"]), True)

    print("Профиль пропал с машины")
    pub_adspower.fetch_host_profiles = lambda url, **kw: FakeFetch(
        [rec("46", "Профиль 46", "u46")], active=[])
    got = W.ui_host_refresh_profiles(host_id)
    check("пропавший посчитан", got["missing_now"], 1)
    profs = {p["serial"]: p for p in sql("SELECT * FROM pub_profiles")}
    check("строка НЕ удалена", sorted(profs), ["12", "46"])
    check("помечен датой пропажи", bool(profs["12"]["missing_since"]), True)
    check("вернувшийся чист", profs["46"]["missing_since"], None)

    print("Неудачная опись не трогает справочник")
    before = sql("SELECT serial, missing_since, active_now FROM pub_profiles ORDER BY serial")

    def boom(url, **kw):
        raise pub_adspower.AdsPowerError("нет связи с машиной")
    pub_adspower.fetch_host_profiles = boom
    refused("сбой связи — честный отказ", lambda: W.ui_host_refresh_profiles(host_id),
            502, "AdsPower")
    check("справочник не тронут",
          sql("SELECT serial, missing_since, active_now FROM pub_profiles ORDER BY serial"),
          before)
    W.ui_host_save(host_id, W.HostIn(code="minsk", title="Минск, GPU", adspower_url=""))
    refused("машина без адреса", lambda: W.ui_host_refresh_profiles(host_id),
            400, "адрес")
    check("и тут справочник цел", len(sql("SELECT 1 FROM pub_profiles")), 2)
    W.ui_host_save(host_id, W.HostIn(code="minsk", title="Минск, GPU",
                                     adspower_url="http://127.0.0.1:50325"))

    print("Что видно на экране")
    hosts = W.ui_hosts()["hosts"]
    check("машина одна", len(hosts), 1)
    check("сколько у неё профилей", hosts[0]["profiles_count"], 2)
    check("адрес виден", hosts[0]["adspower_url"], "http://127.0.0.1:50325")
    profiles = W.ui_profiles()["profiles"]
    check("профилей двое", len(profiles), 2)
    check("у каждого видна машина", {p["host"]["code"] for p in profiles}, {"minsk"})
    check("пропавший помечен",
          {p["serial"]: bool(p["missing_since"]) for p in profiles}, {"46": False, "12": True})

    print("Пропавший профиль не теряется у канала, который на него настроен")
    gone = [p for p in sql("SELECT id, serial FROM pub_profiles") if p["serial"] == "12"][0]
    alive = [p for p in sql("SELECT id, serial FROM pub_profiles") if p["serial"] == "46"][0]
    c = sqlite3.connect(DB)
    c.execute("INSERT INTO pub_channels(tenant_id,name,uc,lang,status,profile_id,"
              "host_id,tz,schedule,horizon_days,created,updated) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (TENANT, "Channel A", "UC" + "a" * 22, "pl", "active",
               gone["id"], host_id, "Europe/Warsaw", "{}", 14,
               time.time(), time.time()))
    c.commit()
    ch_id = c.execute("SELECT id FROM pub_channels").fetchone()[0]
    c.close()

    plain = W.ui_channel_new()
    check("в общем списке пропавшего нет",
          [p["id"] for p in plain["profiles"]], [alive["id"]])
    forch = W.ui_channel_new(channel_id=ch_id)
    ids = [p["id"] for p in forch["profiles"]]
    check("у настроенного канала он есть", gone["id"] in ids, True)
    marked = [p for p in forch["profiles"] if p["id"] == gone["id"]][0]
    check("и честно помечен", (marked["missing"], marked.get("current")), (True, True))
    check("чужой канал его не получает",
          [p["id"] for p in W.ui_channel_new(channel_id=999)["profiles"]], [alive["id"]])

    print("Убранная машина не предлагается новым каналам")
    W.ui_host_status(host_id, W.HostStatusIn(status="disabled"))
    check("список пуст — машина убрана", W.ui_channel_new()["profiles"], [])
    check("но у канала его профиль остаётся",
          gone["id"] in [p["id"] for p in W.ui_channel_new(channel_id=ch_id)["profiles"]],
          True)

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
