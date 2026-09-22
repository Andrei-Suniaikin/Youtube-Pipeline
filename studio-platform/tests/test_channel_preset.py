#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пресет канала с живой привязкой (решение владельца 18.09.2026).

Канал ссылается на пресет, правка пресета сразу меняет поведение всех каналов на нём.
Значения не копируются в канал: их разрешает вид `pub_channels_live`. Язык, ID, имя,
хэндл, профиль AdsPower, статус и заметка в пресет не входят.

Требует стенд (как `tests/test_web_pult.py`):

    VS_PULT_URL=http://127.0.0.1:8082/studio VS_DEV_DB=… VS_DEV_PASSWORD=… \
    .venv/bin/python tests/test_channel_preset.py
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "server"))

import pub_schema                                                  # noqa: E402

PULT = os.environ.get("VS_PULT_URL", "http://127.0.0.1:8082/studio")
DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
LOGIN = os.environ.get("VS_DEV_LOGIN", "dev")
PASSWORD = os.environ.get("VS_DEV_PASSWORD", "dev-local-only")
TENANT = os.environ.get("VS_DEV_TENANT", "dev")
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def go(self, path, payload=None, method=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(PULT + urllib.parse.quote(path, safe="/?&="),
                                     data=data,
                                     method=method or ("POST" if data is not None else "GET"))
        req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw.startswith(("{", "[")) else raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw or "{}")
            except ValueError:
                return exc.code, raw[:200]


def raw_channel(cid: int) -> dict:
    c = sqlite3.connect(DB, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        return dict(c.execute("SELECT * FROM pub_channels WHERE id=?", (cid,)).fetchone())
    finally:
        c.close()


def live_channel(cid: int) -> dict:
    c = sqlite3.connect(DB, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        return dict(c.execute("SELECT * FROM pub_channels_live WHERE id=?", (cid,)).fetchone())
    finally:
        c.close()


def main() -> int:
    print("Схема: вид над каналом и таблица пресетов")
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    pub_schema.ensure_pub_schema(mem)
    mem.commit()
    cols = {r[1] for r in mem.execute("PRAGMA table_info(pub_channels)")}
    check("у канала появилась ссылка на пресет", "preset_channel_id" in cols, True)
    view_cols = [r[1] for r in mem.execute("PRAGMA table_info(pub_channels_live)")]
    check("вид отдаёт все колонки канала плюс имя пресета",
          set(cols) <= set(view_cols) and "preset_channel_name" in view_cols, True)
    check("повторный запуск схемы не падает",
          (pub_schema.ensure_pub_schema(mem), True)[1], True)
    now = time.time()
    mem.execute("INSERT INTO pub_channels(id,tenant_id,uc,name,tz,schedule,horizon_days,"
                "lead_days,created,updated) VALUES(1,'t','UC1','К','Europe/Warsaw',"
                "'{\"mon\": [\"18:00\"]}',3,2,?,?)", (now, now))
    mem.execute("INSERT INTO pub_channel_presets(id,tenant_id,name,tz,lead_days,created,"
                "updated) VALUES(9,'t','П','Europe/Berlin',5,?,?)", (now, now))
    mem.execute("UPDATE pub_channels SET preset_channel_id=9 WHERE id=1")
    row = mem.execute("SELECT * FROM pub_channels_live WHERE id=1").fetchone()
    check("заданное в пресете перекрывает канал", (row["tz"], row["lead_days"]),
          ("Europe/Berlin", 5))
    check("не заданное в пресете остаётся своим",
          (row["schedule"], row["horizon_days"]), ('{"mon": ["18:00"]}', 3))
    check("ссылка чужого тенанта ничего не перекрывает",
          (mem.execute("UPDATE pub_channel_presets SET tenant_id='чужой' WHERE id=9"),
           mem.execute("SELECT tz FROM pub_channels_live WHERE id=1").fetchone()["tz"])[1],
          "Europe/Warsaw")
    try:
        mem.execute("UPDATE pub_channels_live SET tz='X' WHERE id=1")
        check("запись через вид запрещена", "прошла", "ошибка")
    except sqlite3.OperationalError:
        check("запись через вид запрещена", "ошибка", "ошибка")
    mem.close()

    me = Client()
    code, _ = me.go("/ui/login", {"login": LOGIN, "password": PASSWORD})
    if code != 200:
        raise SystemExit(f"вход не удался ({code}). Задайте VS_DEV_PASSWORD.")

    print("\nЖивая привязка: правка пресета меняет канал без записи в канал")
    name = f"проба-канальный-{int(time.time())}"
    code, made = me.go("/ui/channel-presets", {
        "name": name,
        "values": {"schedule_days": ["mon", "thu"], "schedule_times": "09:00",
                   "tz": "Europe/Berlin", "horizon_days": "7", "lead_days": "4",
                   "deferred": "1", "deferred_limit": "3", "niche": "проба"}})
    check("пресет канала заведён", code, 200)
    pid = (made or {}).get("id")
    before = raw_channel(1)
    try:
        check("имя занято — 409", me.go("/ui/channel-presets", {"name": name})[0], 409)
        check("пустое имя — 400", me.go("/ui/channel-presets", {"name": " "})[0], 400)
        check("канал посажен на пресет",
              me.go("/ui/channels/1/channel-preset", {"preset_id": pid})[0], 200)
        live = live_channel(1)
        check("канал теперь живёт по пресету",
              (live["tz"], live["horizon_days"], live["lead_days"], live["niche"]),
              ("Europe/Berlin", 7, 4, "проба"))
        check("собственные настройки канала не переписаны",
              {k: raw_channel(1)[k] for k in ("tz", "horizon_days", "lead_days", "niche")},
              {k: before[k] for k in ("tz", "horizon_days", "lead_days", "niche")})

        # Это и есть «живая» привязка: правим пресет — канал меняется сразу.
        me.go(f"/ui/channel-presets/{pid}", {"name": name, "values": {
            "schedule_days": ["fri"], "schedule_times": "21:30", "tz": "Europe/Kyiv",
            "horizon_days": "10", "lead_days": "1", "deferred": "0",
            "deferred_limit": "9", "niche": "проба-2"}})
        live = live_channel(1)
        check("правка пресета видна каналу сразу",
              (live["tz"], live["horizon_days"], json.loads(live["schedule"])),
              ("Europe/Kyiv", 10, {"fri": ["21:30"]}))

        code, ch = me.go("/ui/channels/1")
        check("экран канала называет пресет", ch["channel_preset"]["name"], name)
        check("и перечисляет, что из него приходит",
              set(ch["channel_preset"]["inherited"]) >= {"tz", "schedule", "horizon_days",
                                                         "lead_days", "niche"}, True)
        check("рядом лежат собственные значения канала — форме есть что сохранить",
              ch["channel_preset"]["own"]["tz"], before["tz"])

        check("занятый пресет удалить нельзя",
              me.go(f"/ui/channel-presets/{pid}", method="DELETE")[0], 409)
        check("канал снят с пресета",
              me.go("/ui/channels/1/channel-preset", {"preset_id": None})[0], 200)
        after = live_channel(1)
        check("после снятия канал вернулся к своим настройкам",
              (after["tz"], after["horizon_days"], after["lead_days"], after["niche"]),
              (before["tz"], before["horizon_days"], before["lead_days"], before["niche"]))
        check("пустой пресет теперь удаляется",
              me.go(f"/ui/channel-presets/{pid}", method="DELETE")[0], 200)
        pid = None
    finally:
        if pid:
            me.go("/ui/channels/1/channel-preset", {"preset_id": None})
            me.go(f"/ui/channel-presets/{pid}", method="DELETE")
    # Сравниваем настройки, а не всю строку: `updated` меняется от самой привязки, а
    # `preset_channel_id` на стенде мог быть уже задан раньше — это не настройка канала.
    keep = [k for k in before if k not in ("updated", "preset_channel_id")]
    check("настройки канала остались как были",
          {k: raw_channel(1)[k] for k in keep}, {k: before[k] for k in keep})
    check("канал не остался привязанным к пресету теста",
          raw_channel(1)["preset_channel_id"], None)

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
