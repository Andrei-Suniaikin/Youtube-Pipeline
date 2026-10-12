#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ссылка для профиля AdsPower: подключить статистику без входа в студию (12.10.2026).

Андрей: каждый профиль AdsPower открывается отдельным браузером, где в студию никто не
входил, и оператору неудобно. Ссылка копируется в студии, открывается в профиле, Google
по очереди спрашивает согласие за каждый канал профиля. Проверяется: ссылку даёт только
администратор; без входа — окно согласия и страница итога с кнопкой «Дальше»; чужой
аккаунт не кладёт токен; «Пропустить»; метка Google одноразовая; ссылка переживает
перезапуск, но не подделку и не срок; старая кнопка по-прежнему требует администратора.

    .venv/bin/python tests/test_analytics_profile_link.py
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
TMP = tempfile.mkdtemp(prefix="oauth-link-")
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
UC2 = "UC" + "b" * 22
_c.execute("INSERT INTO pub_channels(tenant_id,uc,name,lang,tz,schedule,deferred,"
           "deferred_limit,created,updated) VALUES('t1',?,'Второй канал','pl','Europe/Warsaw',"
           "'{}',1,0,?,?)", (UC2, NOW, NOW))
CID2 = _c.execute("SELECT id FROM pub_channels WHERE uc=?", (UC2,)).fetchone()[0]
_c.execute("INSERT INTO pub_profiles(tenant_id,serial,title,created) VALUES('t1','13','рыба',?)",
           (NOW,))
PID = _c.execute("SELECT id FROM pub_profiles WHERE serial='13'").fetchone()[0]
_c.execute("UPDATE pub_channels SET profile_id=?", (PID,))
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
    {"ok": True} if O._read(cid).get("refresh") == GOOGLE["refresh"]
    and not GOOGLE["refresh"].startswith("1//чужой")
    else {"ok": False, "why": "доступ выдан не этим каналом — войдите аккаунтом канала"})
An.collect_channel = lambda c, cid, uc, **kw: {"days": 365, "why": ""}

client = TestClient(W.app, raise_server_exceptions=False)


def as_(sid):
    client.cookies.clear()
    client.cookies.set(W.auth.COOKIE, sid)



def go(path):
    """Как профиль AdsPower: без входа в студию."""
    client.cookies.clear()
    return client.get(path, follow_redirects=False)


def google_state(r):
    loc = r.headers.get("location", "")
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(loc).query)).get("state", ""), loc


def main() -> int:
    print("Ссылку берёт только администратор")
    client.cookies.clear()
    check("без входа — 401", client.post("/ui/analytics/link", json={"channels": [CID]}).status_code, 401)
    as_("s-user")
    check("оператору — 403", client.post("/ui/analytics/link", json={"channels": [CID]}).status_code, 403)

    print("\nГруппы по профилям для главной страницы")
    as_("s-admin")
    g = client.get("/ui/analytics/groups").json()
    check("один профиль №13, два канала", [(x["serial"], len(x["channels"])) for x in g["groups"]],
          [("13", 2)])
    check("подключено 0 из 2", (g["connected"], g["total"]), (0, 2))

    print("\nСсылка на профиль")
    r = client.post("/ui/analytics/link", json={"channels": [CID, CID2]})
    out = r.json()
    check("ответ", (r.status_code, out.get("channels")), (200, 2))
    url = out.get("url", "")
    check("короткий адрес у нас", url.startswith("https://vovastudio.work/studio/oauth/go/"), True)
    token = url.rsplit("/", 1)[-1]
    mode = os.stat(os.path.join(os.environ["VS_CHANNEL_TOKENS_DIR"], "links.json")).st_mode & 0o777
    check("файл ссылок закрыт (0600)", oct(mode), oct(0o600))

    print("\nПрофиль AdsPower, входа в студию нет: первый канал")
    r = go(f"/oauth/go/{token}")
    st, loc = google_state(r)
    check("уводит в окно согласия Google", (r.status_code, loc.startswith(O.AUTH_URL)), (303, True))
    r = go("/oauth/callback?" + urllib.parse.urlencode({"state": st, "code": "4/код"}))
    check("страница итога, не редирект на вход", r.status_code, 200)
    check("«Канал» подключён", "«Канал» подключён" in r.text, True)
    check("кнопка на следующий канал", "Дальше: «Второй канал»" in r.text, True)
    check("токен лежит", O.state(CID)["connected"], True)

    print("\nВторой канал: вошли не тем аккаунтом")
    GOOGLE["refresh"] = "1//чужой-канал-токен-zzzz"
    st, _ = google_state(go(f"/oauth/go/{token}"))
    check("берёт следующий — второй канал", O._PENDING[st]["channel"], CID2)
    r = go("/oauth/callback?" + urllib.parse.urlencode({"state": st, "code": "4/код"}))
    check("не подключён, словами", ("не подключён" in r.text, "не этим каналом" in r.text),
          (True, True))
    check("есть «Повторить» и «Пропустить»", ("Повторить" in r.text, f"skip={CID2}" in r.text),
          (True, True))
    check("токен не остался", O.state(CID2)["connected"], False)
    GOOGLE["refresh"] = "1//refresh-токен-канала-abcd"

    print("\nТа же метка Google второй раз — отказ")
    r = go("/oauth/callback?" + urllib.parse.urlencode({"state": st, "code": "4/код"}))
    check("не принимается", (r.status_code, O.state(CID2)["connected"]), (303, False))

    print("\nПропустить → всё пройдено")
    r = go(f"/oauth/go/{token}?skip={CID2}")
    check("готово", (r.status_code, "Готово" in r.text), (200, True))

    print("\nСсылку на уже подключённые каналы не дают")
    as_("s-admin")
    r = client.post("/ui/analytics/link", json={"channels": [CID]})
    check("отказ словами", (r.status_code, "уже подключены" in r.json().get("detail", "")), (400, True))
    check("канал без ID YouTube / чужой — отказ",
          client.post("/ui/analytics/link", json={"channels": [99999]}).status_code, 400)

    print("\nСсылка переживает перезапуск, но не подделку и не срок")
    r = client.post("/ui/analytics/link", json={"channels": [CID2]})
    token2 = r.json()["url"].rsplit("/", 1)[-1]
    O._PENDING.clear()
    check("после перезапуска работает", go(f"/oauth/go/{token2}").status_code, 303)
    check("подделанная — страница отказа", "не работает" in go("/oauth/go/xxx").text, True)
    check("старше суток — отказ", O.link_left(token2, now=time.time()) == [CID2]
          and _expired(token2), True)

    print("\nКнопка в студии по-прежнему требует того же администратора")
    as_("s-admin")
    b = client.post(f"/ui/channels/{CID2}/analytics/google").json()["url"]
    st = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(b).query))["state"]
    r = go("/oauth/callback?" + urllib.parse.urlencode({"state": st, "code": "4/код"}))
    check("без входа — отказ, токен не лёг",
          (r.status_code, O.state(CID2)["connected"]), (303, False))

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


def _expired(token):
    try:
        O.link_left(token, now=time.time() + O.LINK_SEC + 60)
    except O.OAuthError:
        return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
