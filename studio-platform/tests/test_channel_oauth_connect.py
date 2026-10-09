#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Подключение канала кнопкой «Подключить через Google» (09.10.2026).

Для проверки приложения Google адрес возврата должен быть на нашем домене, а демо-видео —
показывать наш поток согласия, а не служебную страницу OAuth Playground. Здесь — весь
круг на подставном Google: кнопка → окно согласия с нашим адресом возврата и двумя
правами → возврат с кодом → обмен → проверка, что доступ от ЭТОГО канала → назад на
вкладку статистики. И отказы: чужой канал, снятая галочка, отмена, повтор ссылки,
чужой человек.

    .venv/bin/python tests/test_channel_oauth_connect.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="oauth-connect-")
DB = os.path.join(TMP, "vsgen.db")
os.environ["VS_DEV_DB"] = DB
os.environ["VS_SERVER_DB"] = DB
os.environ["VS_DEV_TENANT"] = "t1"
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
os.environ["VS_CHANNEL_TOKENS_DIR"] = os.path.join(TMP, "tokens")
os.environ["VS_YT_OAUTH_CLIENT_ID"] = "client-для-теста.apps.googleusercontent.com"
os.environ["VS_YT_OAUTH_CLIENT_SECRET"] = "secret-для-теста"
os.environ.pop("VS_YT_OAUTH_REDIRECT", None)
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
_c.execute("INSERT INTO users(id,name,role,tenant_id) VALUES(2,'второй','admin','t1')")
_c.execute("INSERT INTO users(id,name,role,tenant_id) VALUES(3,'оператор','user','t1')")
for sid, uid in (("s-admin", 1), ("s-admin2", 2), ("s-user", 3)):
    _c.execute("INSERT INTO sessions(id,user_id,expires) VALUES(?,?,?)", (sid, uid, NOW + 3600))
UC = "UC" + "a" * 22
_c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,tz,schedule,deferred,"
           "deferred_limit,created,updated) VALUES('t1',?,'Канал','pl','Europe/Warsaw',"
           "'{}',1,0,?,?)", (UC, NOW, NOW))
CID = _c.execute("SELECT id FROM pub_channels WHERE uc=?", (UC,)).fetchone()[0]
_c.commit()
_c.close()

import app as W                                                    # noqa: E402
import channel_oauth as O                                          # noqa: E402
import pub_analytics as An                                         # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402

FAILS = []
BOTH = " ".join(O.SCOPES)


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


# Подставной Google: обмен кода и отзыв.
GOOGLE = {"scope": BOTH, "refresh": "1//refresh-токен-канала-abcd", "calls": []}


def fake_post(url, fields):
    GOOGLE["calls"].append((url, dict(fields)))
    if url == O.TOKEN_URL:
        return {"access_token": "ya29.x", "scope": GOOGLE["scope"],
                "refresh_token": GOOGLE["refresh"]}
    return {}


O._post = fake_post
# Принадлежность каналу: «наш» токен — от этого канала, любой другой — нет.
An.verify = lambda cid, uc, **kw: (
    {"ok": True} if O._read(cid).get("refresh") == "1//refresh-токен-канала-abcd"
    else {"ok": False, "why": "доступ выдан не этим каналом — войдите аккаунтом канала"})
An.collect_channel = lambda c, cid, uc, **kw: {"days": 365, "why": ""}

client = TestClient(W.app, raise_server_exceptions=False)


def as_(sid):
    client.cookies.clear()
    client.cookies.set(W.auth.COOKIE, sid)


def begin():
    r = client.post(f"/ui/channels/{CID}/analytics/google")
    url = (r.json() or {}).get("url", "") if r.status_code == 200 else ""
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    return r, url, q


def come_back(**params):
    r = client.get("/oauth/callback?" + urllib.parse.urlencode(params),
                   follow_redirects=False)
    loc = r.headers.get("location", "")
    pre, _, frag = loc.partition("#")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(pre).query))
    return r.status_code, q, frag


def main() -> int:
    print("Кнопка: окно согласия Google")
    as_("s-admin")
    r, url, q = begin()
    check("ответ — адрес Google", url.startswith("https://accounts.google.com/o/oauth2/v2/auth?"), True)
    check("наш client_id", q.get("client_id"), os.environ["VS_YT_OAUTH_CLIENT_ID"])
    check("возврат на наш домен", q.get("redirect_uri"),
          "https://vovastudio.work/studio/oauth/callback")
    check("ровно два права только для чтения", sorted(q.get("scope", "").split()), sorted(O.SCOPES))
    check("долгий токен и выбор аккаунта",
          (q.get("access_type"), "select_account" in q.get("prompt", ""),
           "consent" in q.get("prompt", "")), ("offline", True, True))
    check("одноразовая метка есть", len(q.get("state", "")) >= 20, True)

    print("\nВозврат с кодом — канал подключён")
    st, back, frag = come_back(state=q["state"], code="4/код")
    check("назад в студию", (st, back.get("oauth")), (303, "ok"))
    check("на вкладку статистики канала", frag, f"/channel/{CID}/statistika")
    check("и словами", "забрано дней: 365" in back.get("msg", ""), True)
    check("токен лежит", O.state(CID)["connected"], True)
    ex = [f for u, f in GOOGLE["calls"] if u == O.TOKEN_URL][-1]
    check("обмен — с тем же адресом возврата", ex.get("redirect_uri"),
          "https://vovastudio.work/studio/oauth/callback")
    check("секрет приложения наружу в адрес не попал", "secret" in url, False)

    print("\nТа же ссылка второй раз — отказ")
    st, back, _ = come_back(state=q["state"], code="4/код")
    check("отказ", (back.get("oauth"), "устарела" in back.get("msg", "")), ("err", True))

    print("\nВошли не тем аккаунтом — токен не остаётся")
    O.drop(CID)
    GOOGLE["refresh"] = "1//чужой-канал-токен-zzzz"
    _r, _u, q = begin()
    st, back, _ = come_back(state=q["state"], code="4/код")
    check("отказ словами", (back.get("oauth"), "не этим каналом" in back.get("msg", "")),
          ("err", True))
    check("канал не подключён", O.state(CID)["connected"], False)
    GOOGLE["refresh"] = "1//refresh-токен-канала-abcd"

    print("\nСняли галочку на экране Google")
    GOOGLE["scope"] = O.SCOPES[0]
    _r, _u, q = begin()
    st, back, _ = come_back(state=q["state"], code="4/код")
    check("отказ: «сняли галочку»", "сняли галочку" in back.get("msg", ""), True)
    check("канал не подключён", O.state(CID)["connected"], False)
    GOOGLE["scope"] = BOTH

    print("\nОтменили на экране Google")
    _r, _u, q = begin()
    st, back, frag = come_back(state=q["state"], error="access_denied")
    check("«вы отменили»", ("отменили" in back.get("msg", ""), frag),
          (True, f"/channel/{CID}/statistika"))

    print("\nОтвет Google пришёл другому администратору")
    _r, _u, q = begin()
    as_("s-admin2")
    st, back, _ = come_back(state=q["state"], code="4/код")
    check("отказ", (back.get("oauth"), "другим пользователем" in back.get("msg", "")),
          ("err", True))
    check("канал не подключён", O.state(CID)["connected"], False)

    print("\nБез входа и не администратор")
    client.cookies.clear()
    check("кнопка без входа — 401", client.post(f"/ui/channels/{CID}/analytics/google").status_code, 401)
    st, back, _ = come_back(state="x", code="4/код")
    check("возврат без входа — отказ, не токен", back.get("oauth"), "err")
    as_("s-user")
    check("оператору кнопка — 403", client.post(f"/ui/channels/{CID}/analytics/google").status_code, 403)

    print("\nОтключить — согласие отзывается и у Google")
    as_("s-admin")
    _r, _u, q = begin()
    come_back(state=q["state"], code="4/код")
    GOOGLE["calls"].clear()
    r = client.delete(f"/ui/channels/{CID}/analytics")
    check("отключено", (r.status_code, O.state(CID)["connected"]), (200, False))
    check("отзыв ушёл в Google с этим токеном",
          [(u, f.get("token")) for u, f in GOOGLE["calls"]],
          [(O.REVOKE_URL, "1//refresh-токен-канала-abcd")])

    print("\nАдрес возврата переопределяется окружением")
    os.environ["VS_YT_OAUTH_REDIRECT"] = "http://localhost:8090/oauth/callback"
    _r, _u, q = begin()
    check("свой адрес", q.get("redirect_uri"), "http://localhost:8090/oauth/callback")
    os.environ.pop("VS_YT_OAUTH_REDIRECT")

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
