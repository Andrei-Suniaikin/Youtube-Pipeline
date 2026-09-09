#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пульт: вход обязателен, предпросмотр ничего не пишет, действия пересылаются.

Требует поднятого стенда (dev/run.sh) и пароля кабинета из VS_DEV_PASSWORD.
Провайдеров не трогает: все проверки — про доступ и про то, кто выполняет работу.

    .venv/bin/python tests/test_web_pult.py
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))

PULT = os.environ.get("VS_PULT_URL", "http://127.0.0.1:8080/studio")
DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
LOGIN = os.environ.get("VS_DEV_LOGIN", "dev")
# Пароль стенда задаётся тем же способом, что и в dev/provision.py — одним значением
# на оба. Разойтись им нельзя: тест логинится ровно той учёткой, которую provision завёл.
PASSWORD = os.environ.get("VS_DEV_PASSWORD", "dev-local-only")

SCREENS = ("releases", "channels", "queue", "keys", "presets", "alerts", "disk", "public")
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


class Client:
    """Браузер на минималках: помнит куки, отдаёт код и тело."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def go(self, path, payload=None, method=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        # Кириллица в пути: браузер кодирует её сам, urllib — нет.
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

    @property
    def has_session(self) -> bool:
        return any(ck.name == "vs_session" for ck in self.jar)


def plan_rows(url_like: str) -> int:
    c = sqlite3.connect(DB, timeout=5)
    try:
        return c.execute("SELECT COUNT(*) FROM pub_plan_items WHERE donor_url LIKE ?",
                         (f"%{url_like}%",)).fetchone()[0]
    finally:
        c.close()


def project_row(pid: str):
    c = sqlite3.connect(DB, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        return c.execute("SELECT name, dir_path FROM projects WHERE id=?", (pid,)).fetchone()
    finally:
        c.close()


def drop_project(pid: str) -> None:
    """Убрать за собой: тест не должен оставлять проекты на стенде."""
    row = project_row(pid)
    if row and row["dir_path"]:
        shutil.rmtree(row["dir_path"], ignore_errors=True)
    c = sqlite3.connect(DB, timeout=5)
    try:
        c.execute("DELETE FROM projects WHERE id=?", (pid,))
        c.commit()
    finally:
        c.close()


def preset_row(prid: int):
    c = sqlite3.connect(DB, timeout=5)
    try:
        return c.execute("SELECT id FROM presets WHERE id=?", (prid,)).fetchone()
    finally:
        c.close()


def drop_preset(prid: int) -> None:
    c = sqlite3.connect(DB, timeout=5)
    try:
        c.execute("DELETE FROM presets WHERE id=?", (prid,))
        c.commit()
    finally:
        c.close()


def main() -> int:
    anon = Client()
    print("Без входа не работает ничего")
    for screen in SCREENS:
        code, _ = anon.go(f"/ui/{screen}")
        if code != 401:
            check(f"/ui/{screen} закрыт", code, 401)
    check("все экраны закрыты", [c for c in (anon.go(f"/ui/{s}")[0] for s in SCREENS)
                                 if c != 401], [])
    check("страница-оболочка открыта", anon.go("/")[0], 200)
    check("«кто я» отвечает 401, а не ошибкой", anon.go("/ui/me")[0], 401)
    check("действие тоже закрыто", anon.go("/ui/actions/vsgen/1/cancel", {})[0], 401)

    print("\nВход")
    code, body = anon.go("/ui/login", {"login": LOGIN, "password": "не тот"})
    check("неверный пароль не пускает", code, 401)
    check("и не рассказывает, что именно не так",
          body.get("detail"), "неверный логин или пароль")
    check("сессии не появилось", anon.has_session, False)

    me = Client()
    code, body = me.go("/ui/login", {"login": LOGIN, "password": PASSWORD})
    if code != 200:
        raise SystemExit(f"вход не удался ({code}): {body}. "
                         f"Задайте VS_DEV_PASSWORD или запустите dev/provision.py")
    check("верный пароль пускает", code, 200)
    check("сессия выдана", me.has_session, True)
    check("кто вошёл", me.go("/ui/me")[1].get("name"), LOGIN)

    print("\nС сессией открыты все экраны")
    bad = [s for s in SCREENS if me.go(f"/ui/{s}")[0] != 200]
    check("ни один экран не отказал", bad, [])

    print("\nПредпросмотр не оставляет следов")
    mark = "test-preview-does-not-write"
    before = plan_rows(mark)
    code, body = me.go("/ui/links/preview",
                       {"channel_id": 1, "links": f"https://youtu.be/{mark}"})
    check("предпросмотр отвечает", code, 200)
    check("сказано, чем будет сделано", len(body.get("presets") or []), 3)
    check("в базе ничего не прибавилось", plan_rows(mark), before)

    print("\nДействия пересылаются, а не выполняются здесь")
    code, body = me.go("/ui/actions/vsgen/99999999/cancel", {})
    check("чужая задача не отменяется", code in (403, 404), True)
    code, body = me.go("/ui/actions/vsgen/1/удалить", {})
    check("несуществующее действие отвергнуто", code, 404)
    code, body = me.go("/ui/actions/наш-сервис/1/cancel", {})
    check("несуществующий сервис отвергнут", code, 404)
    code, body = me.go("/ui/actions/rewrite/нет-такой/cancel", {})
    check("ответ соседа пересказан как есть", code, 404)
    check("и его словами", body.get("detail"), "job not found")

    print("\nПроект заводится в пульте, а работу ставит кабинет")
    check("пустой текст не принимается", me.go("/ui/projects", {"text": "   "})[0], 400)
    check("завести проект без входа нельзя",
          anon.go("/ui/projects", {"text": "раз два три"})[0], 401)
    name = "тест пульта: не удалять руками"
    code, body = me.go("/ui/projects", {"name": name, "text": "Первая фраза. Вторая фраза."})
    check("проект заведён", code, 200)
    pid = str((body or {}).get("id") or "")
    check("номер вернулся", pid.startswith("p_"), True)
    if pid:
        row = project_row(pid)
        check("проект лежит в базе кабинета", row is not None, True)
        # Кабинет берёт название из первой строки текста и её же озвучивает. В пульте
        # это два разных поля, и название не должно подменяться текстом.
        check("название своё, а не первая строка", row["name"] if row else "", name)
        check("запуск закрыт без входа", anon.go(f"/ui/projects/{pid}/run", {})[0], 401)
        drop_project(pid)
        check("проект убран за собой", project_row(pid) is None, True)
    # Несуществующий проект: кабинет отвечает как обычно и не ставит ничего. Это
    # проверка, что запуск действительно уходит к нему, а не решается здесь.
    code, body = me.go("/ui/projects/нет-такого-проекта/run", {})
    check("чужой проект ничего не ставит", (code, (body or {}).get("enqueued")), (200, 0))

    print("\nПресет заводится и правится здесь, а проверяют его контракты кабинета")
    pname = "тест пульта: пресет"
    code, body = me.go("/ui/presets/vsgen", {"name": pname})
    check("пресет заведён", code, 200)
    prid = (body or {}).get("id")
    check("номер вернулся", isinstance(prid, int), True)
    check("второй с тем же именем не заводится",
          me.go("/ui/presets/vsgen", {"name": pname})[0], 409)
    check("без имени не заводится", me.go("/ui/presets/vsgen", {"name": "  "})[0], 400)
    if prid:
        code, d = me.go(f"/ui/presets/vsgen/{prid}")
        check("поля пришли", code, 200)
        fields = {f["key"]: f for g in d.get("groups", []) for f in g.get("fields", [])}
        # Варианты выбора берутся из констант кабинета. Пустой список значил бы, что
        # описание оторвалось от кабинета и показывает выдуманное.
        check("модели картинок непустые", len(fields["model"]["options"]) > 3, True)
        check("движки озвучки непустые", len(fields["engine"]["options"]) > 1, True)
        # Поле, которого форма не знает, не должно молча уехать в настройки пресета.
        check("чужое поле отвергнуто",
              me.go(f"/ui/presets/vsgen/{prid}", {"name": pname,
                                                  "values": {"нет_такого": "1"}})[0], 400)
        check("сохранение принято",
              me.go(f"/ui/presets/vsgen/{prid}",
                    {"name": pname, "values": {"model": "NARWHAL",
                                               "sentences_per_prompt": "4"}})[0], 200)
        back = {f["key"]: f["value"] for g in me.go(f"/ui/presets/vsgen/{prid}")[1]["groups"]
                for f in g["fields"]}
        check("значение сохранилось", back.get("sentences_per_prompt"), "4")
        # Правило 2 ТЗ: seed принимают не все модели, и поле обязано об этом сказать.
        check("seed у модели, которая его принимает, жив",
              me.go(f"/ui/presets/vsgen/{prid}")[1] and
              [f for g in me.go(f"/ui/presets/vsgen/{prid}")[1]["groups"]
               for f in g["fields"] if f["key"] == "seed"][0]["off"], "")
        me.go(f"/ui/presets/vsgen/{prid}", {"name": pname, "values": {"model": "OPENAI"}})
        off = [f for g in me.go(f"/ui/presets/vsgen/{prid}")[1]["groups"]
               for f in g["fields"] if f["key"] == "seed"][0]["off"]
        check("а у той, что не принимает, гаснет с причиной", bool(off), True)
        drop_preset(prid)
        check("пресет убран за собой", preset_row(prid) is None, True)
    check("чужого пресета нет", me.go("/ui/presets/vsgen/999999")[0], 404)

    print("\nСтраница зовёт API по своему адресу, а не по корню платформы")
    # Тот самый случай, который сломал вход и не ловился curl'ом: страница живёт под
    # /studio, а зовёт «/ui/…». Браузер резолвит такой путь от корня — то есть в кабинет.
    code, page = me.go("/")
    check("страница отдаётся", code, 200)
    check("база API берётся из адреса", "const API = (location.pathname.match" in page,
          True)
    bare = [line.strip()[:70] for line in page.splitlines()
            if ("fetch(" in line and "API + " not in line and "fetch(API" not in line)
            or ("'/ui/files/" in line and "${API}" not in line)]
    check("ни одного вызова мимо базы", bare, [])

    print("\nВыход")
    check("выход принят", me.go("/ui/logout", {})[0], 200)
    check("после выхода снова закрыто", me.go("/ui/releases")[0], 401)

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
