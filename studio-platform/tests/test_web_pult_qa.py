#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QA фазы 2 батча «Пресеты целые и частичные»: независимая проверка API пульта
поверх того, что уже покрыто `tests/test_web_pult.py`.

Здесь — сценарии, которых основной тест пульта не проверяет напрямую:
- `GET /ui/presets/vsgen/{id}` у САМОГО частичного пресета отдаёт свой `scope`
  (основной тест проверяет это только в ответе на создание и в списке, не в форме
  частичного);
- `POST /ui/presets/vsgen/{id}/links` отвергает частичный пресет ЧУЖОГО тенанта —
  проверка тенанта у `resolve()`/`users_of()` уже есть в `test_preset_links.py`
  (уровень функций), а здесь то же самое поверх реального HTTP пути пульта;
- `GET /ui/presets` (список) считает `users.projects` — основной тест проверяет
  в списке только `users.channels`, а `users.projects` — только в форме одного
  пресета (`users_of` целиком), не в сводке списка.

Требует поднятого стенда (dev/run.sh) и пароля кабинета из VS_DEV_PASSWORD.

    .venv/bin/python tests/test_web_pult_qa.py
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))

PULT = os.environ.get("VS_PULT_URL", "http://127.0.0.1:8080/studio")
PLATFORM = os.environ.get("VS_PLATFORM_BASE",
                          PULT[: -len("/studio")] if PULT.endswith("/studio") else PULT)
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
    """Тот же минимальный «браузер», что и в `test_web_pult.py` — курс проекта не
    заводит общий модуль-помощник для тестов, каждый тест самодостаточен."""

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
            with self.opener.open(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw.startswith(("{", "[")) else raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw or "{}")
            except ValueError:
                return exc.code, raw[:200]
        except urllib.error.URLError as exc:
            raise SystemExit(f"пульт {PULT} не отвечает: {exc.reason}. Сначала: dev/run.sh")


def preset_row(prid: int):
    c = sqlite3.connect(DB, timeout=5)
    try:
        return c.execute("SELECT id FROM presets WHERE id=?", (prid,)).fetchone()
    finally:
        c.close()


def drop_preset(prid) -> None:
    if not prid:
        return
    c = sqlite3.connect(DB, timeout=5)
    try:
        c.execute("DELETE FROM presets WHERE id=?", (prid,))
        c.commit()
    finally:
        c.close()


def insert_foreign_partial(name: str, part: str, tenant: str) -> int:
    """Частичный пресет чужого тенанта — заводится напрямую в базе: ручка API
    всегда пишет пресет в `TENANT` текущего пульта, чужого тенанта так не завести."""
    c = sqlite3.connect(DB, timeout=5)
    try:
        settings = json.dumps({"_scope": part}, ensure_ascii=False)
        cur = c.execute(
            "INSERT INTO presets(tenant_id, name, settings) VALUES(?, ?, ?)",
            (tenant, name, settings))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def project_row(pid: str):
    c = sqlite3.connect(DB, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        return c.execute("SELECT name, dir_path FROM projects WHERE id=?", (pid,)).fetchone()
    finally:
        c.close()


def drop_project(pid) -> None:
    if not pid:
        return
    row = project_row(pid)
    if row and row["dir_path"]:
        shutil.rmtree(row["dir_path"], ignore_errors=True)
    c = sqlite3.connect(DB, timeout=5)
    try:
        c.execute("DELETE FROM projects WHERE id=?", (pid,))
        c.commit()
    finally:
        c.close()


def cabinet_apply_preset(base_url: str, cookie_header: str, prid: int, pid: str):
    """`POST /api/presets/{prid}/apply` кабинета напрямую — тем же приёмом, что и
    в `test_web_pult.py` (единая сессия `vs_session`, тело form-urlencoded)."""
    data = urllib.parse.urlencode({"pid": pid}).encode("utf-8")
    req = urllib.request.Request(base_url + f"/api/presets/{prid}/apply",
                                 data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Cookie", cookie_header)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def session_cookie(client: Client) -> str:
    for ck in client.jar:
        if ck.name == "vs_session":
            return ck.value or ""
    return ""


def main() -> int:
    me = Client()
    code, body = me.go("/ui/login", {"login": LOGIN, "password": PASSWORD})
    if code != 200:
        raise SystemExit(f"вход не удался ({code}): {body}. "
                         f"Задайте VS_DEV_PASSWORD или запустите dev/provision.py")

    print("GET формы у самого частичного пресета отдаёт его собственный scope "
          "(основной тест пульта проверяет scope частичного только в ответе на "
          "создание и в списке, не в его собственной форме)")
    p_name = "QA2: собственный scope частичного в форме"
    code, body = me.go("/ui/presets/vsgen", {"name": p_name, "scope": "clips"})
    check("частичный (clips) заведён", code, 200)
    pid = (body or {}).get("id")
    if pid:
        code, d = me.go(f"/ui/presets/vsgen/{pid}")
        check("форма частичного открылась", code, 200)
        check("scope формы частичного — его часть, а не full", d.get("scope"), "clips")
        check("links у частичного пуст (ссылок на ссылки нет)", d.get("links", {}), {})
        drop_preset(pid)
        check("убран за собой", preset_row(pid) is None, True)

    print("\nСсылка на частичный пресет ЧУЖОГО тенанта отвергается через API "
          "(того же тенанта — явное требование раздела «3. API пульта»; юнит-тест "
          "`test_preset_links.py` проверяет то же правило внутри `resolve()`/"
          "`users_of()`, здесь — тот же вопрос поверх реального HTTP пути пульта)")
    foreign_id = insert_foreign_partial("QA2: чужой тенант, частичный images",
                                        "images", "чужой-тенант-qa2")
    full_name = "QA2: целый против чужого тенанта"
    code, body = me.go("/ui/presets/vsgen", {"name": full_name})
    check("свой целый заведён", code, 200)
    full_id = (body or {}).get("id")
    if full_id:
        code, _ = me.go(f"/ui/presets/vsgen/{full_id}/links",
                        {"part": "images", "preset_id": foreign_id})
        check("ссылка на частичный чужого тенанта отвергнута (не найден в своём тенанте)",
              code, 404)
        code, d = me.go(f"/ui/presets/vsgen/{full_id}")
        check("после отказа ссылка не встала", d.get("links", {}).get("images"), None)
        drop_preset(full_id)
    drop_preset(foreign_id)

    print("\nСписок пресетов считает users.projects (основной тест пульта в списке "
          "проверяет только users.channels; users.projects там же — отдельный путь "
          "подсчёта из `users_of()`, до сих пор проверенный только в форме одного "
          "пресета, не в сводке списка)")
    partial_name = "QA2: частичный для users.projects в списке"
    code, body = me.go("/ui/presets/vsgen", {"name": partial_name, "scope": "images"})
    partial_id = (body or {}).get("id")
    full_name2 = "QA2: целый для users.projects в списке"
    code, body = me.go("/ui/presets/vsgen", {"name": full_name2})
    full2_id = (body or {}).get("id")
    proj_id = None
    if partial_id and full2_id:
        me.go(f"/ui/presets/vsgen/{full2_id}/links",
              {"part": "images", "preset_id": partial_id})
        code, body = me.go("/ui/projects",
                           {"name": "QA2: проект для users.projects в списке",
                            "text": "Раз. Два."})
        check("проект заведён", code, 200)
        proj_id = str((body or {}).get("id") or "")
        if proj_id:
            sid = session_cookie(me)
            status = cabinet_apply_preset(PLATFORM, f"vs_session={sid}", full2_id, proj_id)
            check("кабинет принял применение пресета", status, 200)

            code, d = me.go("/ui/presets")
            vsgen_group = next(g for g in d["groups"] if g["service"] == "vsgen")
            items = {i["key"]: i for i in vsgen_group["items"]}
            check("список пресетов видит users.projects == 1 у целого",
                  items.get(str(full2_id), {}).get("users", {}).get("projects"), 1)
            check("users.projects у пресета, на который никто не завязан, — 0",
                  items.get(str(partial_id), {}).get("users", {}).get("projects"), 0)

            drop_project(proj_id)
        drop_preset(full2_id)
        drop_preset(partial_id)

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
