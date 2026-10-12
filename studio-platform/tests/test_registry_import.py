#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Единый список каналов: загрузка раскладки xlsx (12.10.2026).

Команда решила держать связку «канал → владелец → менеджер → профиль AdsPower» в студии.
Проверяется на подставной раскладке:
  • пробный прогон ничего не пишет;
  • профиль студии со свежими заливками главнее xlsx («xlsx отстал»);
  • спорный канал (заливок нет, xlsx называет другой профиль) — владелец НЕ пишется;
  • `profile_id` — откуда льём — не меняется ни в каком режиме;
  • запись: менеджеры, почта владельца, менеджер, права, копия базы рядом;
  • повторная запись того же файла ничего не меняет;
  • только администратор; не-xlsx — понятный отказ.

    .venv/bin/python tests/test_registry_import.py
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="registry-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "web"))

from pub_schema import ensure_pub_schema                           # noqa: E402

NOW = time.time()
_c = sqlite3.connect(DB)
ensure_pub_schema(_c)
_c.executescript("""
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
    role TEXT, tenant_id TEXT, disabled INTEGER DEFAULT 0);
  CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, user_id INTEGER, expires REAL);
  CREATE TABLE IF NOT EXISTS admin_state(k TEXT PRIMARY KEY, v TEXT, updated REAL);
  CREATE TABLE IF NOT EXISTS queue_tasks(job_id INTEGER PRIMARY KEY, tenant_id TEXT,
    kind TEXT, status TEXT, created REAL, finished REAL, project_id TEXT, error TEXT);
  CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT, tenant_id TEXT,
    settings TEXT);
""")
_c.execute("INSERT INTO users(id,name,role,tenant_id) VALUES(1,'хозяин','admin','t1')")
_c.execute("INSERT INTO users(id,name,role,tenant_id) VALUES(3,'оператор','user','t1')")
for sid, uid in (("s-admin", 1), ("s-user", 3)):
    _c.execute("INSERT INTO sessions(id,user_id,expires) VALUES(?,?,?)", (sid, uid, NOW + 3600))
P = {}
for serial in ("11", "12", "30", "52", "55"):
    _c.execute("INSERT INTO pub_profiles(tenant_id,serial,created) VALUES('t1',?,?)", (serial, NOW))
    P[serial] = _c.execute("SELECT id FROM pub_profiles WHERE serial=?", (serial,)).fetchone()[0]
UC = {k: "UC" + k * 22 for k in "abcd"}


def channel(key, name, serial):
    _c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,tz,schedule,deferred,"
               "deferred_limit,profile_id,created,updated) VALUES('t1',?,?,'pl',"
               "'Europe/Warsaw','{}',1,0,?,?,?)", (UC[key], name, P[serial], NOW, NOW))
    return _c.execute("SELECT id FROM pub_channels WHERE uc=?", (UC[key],)).fetchone()[0]


A = channel("a", "Совпадает", "12")        # студия = владелец по xlsx
B = channel("b", "Студия права", "11")     # xlsx говорит P30, но с №11 свежие заливки
C = channel("c", "Спорный", "55")          # заливок нет, xlsx: владелец P52
D = channel("d", "Нет в xlsx", "12")
_c.execute("INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,profile_id,"
           "channel_url,title,video_path,slot_at,status,created,updated) VALUES('r1','t1',?,?,?,"
           "'u','t','v',0,'done',?,?)", (B, UC["b"], P["11"], NOW, NOW - 3600))
_c.commit()
_c.close()

import app as W                                                    # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def xlsx(sheets) -> bytes:
    """Минимальный xlsx: строки — inline-строки, как пишет любой редактор таблиц."""
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, "w")
    z.writestr("[Content_Types].xml", "<Types/>")
    wb = ['<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>']
    rels = ['<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    for i, (name, rows) in enumerate(sheets, 1):
        wb.append(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>')
        rels.append(f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml" Type="x"/>')
        body = []
        for r, row in enumerate(rows, 1):
            cells = "".join(f'<c r="{chr(65 + j)}{r}" t="inlineStr"><is><t>{v}</t></is></c>'
                            for j, v in enumerate(row) if v != "")
            body.append(f'<row r="{r}">{cells}</row>')
        z.writestr(f"xl/worksheets/sheet{i}.xml",
                   '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   f'<sheetData>{"".join(body)}</sheetData></worksheet>')
    z.writestr("xl/workbook.xml", "".join(wb) + "</sheets></workbook>")
    z.writestr("xl/_rels/workbook.xml.rels", "".join(rels) + "</Relationships>")
    z.close()
    return buf.getvalue()


HEAD = ["№ менеджера", "Заливка из", "Код", "Канал", "Язык", "YouTube / точный UC",
        "Почта владельца", "Где владелец по v3", "Права Manager", "Studio на Минске",
        "Видео >15 мин", "Комментарий"]
BOOK = xlsx([
    ("Менеджеры", [["Менеджеры: шапка"], [],
                   ["№", "Профиль", "Короткое имя", "Почта менеджера", "Каналов", "Права приняты"],
                   ["07", "P30", "m07", "m07@example.com", "2", "Нет"]]),
    ("Каналы", [["Каналы: шапка"], [], HEAD,
                ["07", "P30", "A1", "Совпадает", "pl", f"https://youtube.com/channel/{UC['a']}",
                 "own-a@example.com", "P12", "Не отправлено", "", "да", ""],
                ["07", "P30", "B1", "Студия права", "pl", UC["b"],
                 "own-b@example.com", "P30", "Отправлено", "", "", ""],
                ["07", "P55", "C1", "Спорный", "pl", UC["c"],
                 "own-c@example.com", "P52", "Не отправлено", "", "нет", ""],
                ["07", "P30", "M1", "Манхва", "ru", "UC" + "z" * 22,
                 "own-m@example.com", "P30", "", "", "", ""]]),
])


def send(data, apply=0, sid="s-admin"):
    client.cookies.clear()
    client.cookies.set(W.auth.COOKIE, sid)
    return client.post("/ui/registry/import" + ("?apply=1" if apply else ""),
                       files={"file": ("p.xlsx", data, "application/octet-stream")})


def row(cid):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        return dict(c.execute("SELECT * FROM pub_channels WHERE id=?", (cid,)).fetchone())
    finally:
        c.close()


client = TestClient(W.app, raise_server_exceptions=False)


def main() -> int:
    print("Только администратор, только xlsx")
    check("оператору — 403", send(BOOK, sid="s-user").status_code, 403)
    r = send(b"not a zip")
    check("не xlsx — 400 словами", (r.status_code, "не xlsx" in r.json().get("detail", "")),
          (400, True))

    print("\nПробный прогон")
    r = send(BOOK)
    rep = r.json()["report"]
    check("ответ", r.status_code, 200)
    check("каналов изменится", rep["channels_changed"], 3)
    check("спорный — один, «Спорный»", [d["channel"] for d in rep["disputed"]], ["Спорный"])
    check("xlsx отстал — «Студия права»", [(d["channel"], d["studio"], d["xlsx_owner"])
                                           for d in rep["xlsx_stale"]], [("Студия права", "11", "30")])
    check("нет в студии — манхва", len(rep["not_in_studio"]), 1)
    check("нет в xlsx", [x.split(" (")[0] for x in rep["not_in_xlsx"]], ["Нет в xlsx"])
    check("ничего не записано", (row(A)["owner_email"], row(B)["manager_id"]), (None, None))

    print("\nЗапись")
    before = {cid: row(cid)["profile_id"] for cid in (A, B, C, D)}
    r = send(BOOK, apply=1)
    out = r.json()
    check("записано", out.get("applied"), {"managers": 1, "channels": 3})
    check("копия базы рядом", os.path.exists(os.path.join(TMP, out.get("backup", "-"))), True)
    check("откуда льём — не тронуто", {cid: row(cid)["profile_id"] for cid in before}, before)
    a, b, c = row(A), row(B), row(C)
    check("совпадающий: владелец = профиль студии", a["owner_profile_id"], P["12"])
    check("студия права: владелец = профиль студии, не xlsx", b["owner_profile_id"], P["11"])
    check("спорный: владелец НЕ записан", c["owner_profile_id"], None)
    check("почта владельца", (a["owner_email"], c["owner_email"]),
          ("own-a@example.com", "own-c@example.com"))
    check("права: «Отправлено» → sent, есть дата", (b["mgr_rights"], b["mgr_rights_at"] is not None),
          ("sent", True))
    check("права: «Не отправлено» → пусто", a["mgr_rights"], "")
    check("видео >15 мин: да / нет / неизвестно", (a["long_ok"], c["long_ok"], b["long_ok"]),
          (1, 0, None))
    con = sqlite3.connect(DB)
    m = con.execute("SELECT num, email, profile_id FROM pub_managers").fetchall()
    check("менеджер №7 на профиле P30", m, [(7, "m07@example.com", P["30"])])
    check("каналы привязаны к менеджеру", a["manager_id"] is not None and a["manager_id"] == b["manager_id"],
          True)
    con.close()

    print("\nПовтор того же файла")
    rep = send(BOOK).json()["report"]
    check("менять нечего", (rep["channels_changed"], rep["managers_new"], rep["managers_changed"]),
          (0, 0, 0))

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
