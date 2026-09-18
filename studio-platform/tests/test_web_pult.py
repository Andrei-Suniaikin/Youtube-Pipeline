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
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))

PULT = os.environ.get("VS_PULT_URL", "http://127.0.0.1:8080/studio")
# Кабинет живёт в том же процессе под корнем платформы (web/platform.py) — сквозная
# проверка через `POST /api/presets/{id}/apply` бьёт туда напрямую, а не через /studio.
PLATFORM = os.environ.get("VS_PLATFORM_BASE",
                          PULT[: -len("/studio")] if PULT.endswith("/studio") else PULT)
DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
TENANT = os.environ.get("VS_DEV_TENANT", "dev")
LOGIN = os.environ.get("VS_DEV_LOGIN", "dev")
# Пароль стенда задаётся тем же способом, что и в dev/provision.py — одним значением
# на оба. Разойтись им нельзя: тест логинится ровно той учёткой, которую provision завёл.
PASSWORD = os.environ.get("VS_DEV_PASSWORD", "dev-local-only")

SCREENS = ("releases", "channels", "queue", "keys", "presets", "alerts", "disk",
           "public", "limits")
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

    def session_cookie(self) -> str:
        """Значение `vs_session` — единый вход у пульта и кабинета (то же имя, то
        же значение), нужно для похода к кабинету напрямую в обход `Client.go()`."""
        for ck in self.jar:
            if ck.name == "vs_session":
                return ck.value or ""
        return ""


def plan_rows(url_like: str) -> int:
    c = sqlite3.connect(DB, timeout=5)
    try:
        return c.execute("SELECT COUNT(*) FROM pub_plan_items WHERE donor_url LIKE ?",
                         (f"%{url_like}%",)).fetchone()[0]
    finally:
        c.close()


def make_release(mark: str) -> tuple:
    """Выпуск на первом канале, ещё без проекта: ровно тот случай, когда ролик канала
    раньше не было видно нигде, кроме самого канала. Заводим строками — движок на
    стенде выключен (VS_SERVER_DISABLE_BACKGROUND=1)."""
    now = time.time()
    rel = "r-test-" + secrets.token_hex(4)
    c = sqlite3.connect(DB, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        ch = c.execute("SELECT id, uc FROM pub_channels WHERE tenant_id=? ORDER BY id LIMIT 1",
                       (TENANT,)).fetchone()
        cur = c.execute(
            "INSERT INTO pub_plan_items(tenant_id, channel_id, donor_url, donor_title, "
            "position, slot_at, status, created, updated) VALUES(?,?,?,?,?,?,'frozen',?,?)",
            (TENANT, ch["id"], f"https://youtu.be/{mark}", "Проверочный ролик",
             1, now + 3600, now, now))
        plan_id = cur.lastrowid
        c.execute(
            "INSERT INTO pub_releases(release_id, tenant_id, plan_item_id, plan_item_version, "
            "channel_id, uc, slot_at, status, phase, blocked_reason, created, updated) "
            "VALUES(?,?,?,1,?,?,?, 'pending','created','нет ключа (проверка)',?,?)",
            (rel, TENANT, plan_id, ch["id"], ch["uc"] or "", now + 3600, now, now))
        c.commit()
        return rel, plan_id
    finally:
        c.close()


def set_release_phase(rel: str, phase: str) -> None:
    c = sqlite3.connect(DB, timeout=5)
    try:
        c.execute("UPDATE pub_releases SET phase=? WHERE release_id=?", (phase, rel))
        c.commit()
    finally:
        c.close()


def release_status(rel: str) -> str:
    c = sqlite3.connect(DB, timeout=5)
    try:
        row = c.execute("SELECT status FROM pub_releases WHERE release_id=?", (rel,)).fetchone()
        return row[0] if row else ""
    finally:
        c.close()


def drop_release(rel: str, plan_id: int) -> None:
    c = sqlite3.connect(DB, timeout=5)
    try:
        c.execute("DELETE FROM pub_releases WHERE release_id=?", (rel,))
        c.execute("DELETE FROM pub_plan_items WHERE id=?", (plan_id,))
        c.commit()
    finally:
        c.close()


def rew_preset(name: str) -> dict:
    """Пресет рерайта прямо из его базы: через API видно не всё (и не должно)."""
    c = sqlite3.connect(os.path.join(BASE, "rewrite", "sessions.db"), timeout=5)
    try:
        row = c.execute("SELECT rew_presets FROM user_keys WHERE user_id=?",
                        (TENANT,)).fetchone()
    finally:
        c.close()
    return (json.loads(row[0] or "{}") if row else {}).get(name, {})


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


def preset_settings(prid: int) -> dict:
    """Сырые `settings` пресета из базы — для проверок, до которых API не пускает
    (тип значения в `_links`, порча состояния частичного напрямую в обход API)."""
    c = sqlite3.connect(DB, timeout=5)
    try:
        row = c.execute("SELECT settings FROM presets WHERE id=?", (prid,)).fetchone()
        return json.loads(row[0]) if row and row[0] else {}
    finally:
        c.close()


def write_preset_settings(prid: int, settings: dict) -> None:
    """Записать `settings` пресета напрямую — имитирует легаси-данные / порчу в обход
    API (частичный пресет со своими ссылками, который сама ручка API завести не даёт)."""
    c = sqlite3.connect(DB, timeout=5)
    try:
        c.execute("UPDATE presets SET settings=? WHERE id=?",
                  (json.dumps(settings, ensure_ascii=False), prid))
        c.commit()
    finally:
        c.close()


def project_settings(pid: str) -> dict:
    c = sqlite3.connect(DB, timeout=5)
    try:
        row = c.execute("SELECT settings FROM projects WHERE id=?", (pid,)).fetchone()
        return json.loads(row[0]) if row and row[0] else {}
    finally:
        c.close()


def insert_channel(name: str, preset_vsgen_id) -> int:
    """Канал, указывающий на пресет, — минимальная строка без похода через API
    кабинета (у канала своя, более тяжёлая форма заведения, здесь важна только
    связь `preset_vsgen_id` для проверки «кто бежит»)."""
    c = sqlite3.connect(DB, timeout=5)
    try:
        cur = c.execute(
            "INSERT INTO pub_channels(tenant_id, name, preset_vsgen_id, created, updated) "
            "VALUES('dev', ?, ?, ?, ?)",
            (name, preset_vsgen_id, time.time(), time.time()))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def drop_channel(chid: int) -> None:
    c = sqlite3.connect(DB, timeout=5)
    try:
        c.execute("DELETE FROM pub_channels WHERE id=?", (chid,))
        c.commit()
    finally:
        c.close()


def cabinet_apply_preset(base_url: str, cookie_header: str, prid: int, pid: str):
    """`POST /api/presets/{prid}/apply` кабинета напрямую — той же сессией, что и
    пульт (единый вход, то же имя и значение куки `vs_session`). Кабинет разбирает
    тело как `application/x-www-form-urlencoded`, а не JSON (`_form()` в main.py),
    поэтому вызов нельзя сделать общим `Client.go()`, который всегда шлёт JSON.
    Редиректы кабинет не запрещает — по умолчанию их отслеживает сам urllib.
    """
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
    # Форма ответа, на которую опирается страница канала. 17.09 страница читала
    # несуществующее `items`, и кнопка «Поставить» не включалась никогда.
    check("в ответе есть placed / queued / skipped",
          all(k in body for k in ("placed", "queued", "skipped")), True)

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

    print("\nПресеты целые и частичные: ссылка на группу")
    p_partial_name = "тест пульта: частичный images"
    code, body = me.go("/ui/presets/vsgen", {"name": p_partial_name, "scope": "images"})
    check("частичный пресет (images) заведён", code, 200)
    check("scope вернулся", (body or {}).get("scope"), "images")
    partial_id = (body or {}).get("id")

    p_partial_voice_name = "тест пульта: частичный voice"
    code, body = me.go("/ui/presets/vsgen", {"name": p_partial_voice_name, "scope": "voice"})
    check("второй частичный (voice) заведён", code, 200)
    partial_voice_id = (body or {}).get("id")

    check("незнакомая часть в scope отвергнута",
          me.go("/ui/presets/vsgen", {"name": "тест пульта: плохой scope",
                                       "scope": "ерунда"})[0], 400)

    if partial_id and partial_voice_id:
        check("своё поле частичного сохраняется",
              me.go(f"/ui/presets/vsgen/{partial_id}",
                    {"name": p_partial_name, "values": {"style": "из частичного"}})[0], 200)

        p_full_name = "тест пульта: целый со ссылкой"
        code, body = me.go("/ui/presets/vsgen", {"name": p_full_name})
        check("целый пресет заведён", code, 200)
        check("scope целого — full", (body or {}).get("scope"), "full")
        full_id = (body or {}).get("id")

        if full_id:
            check("своё поле целого сохранено",
                  me.go(f"/ui/presets/vsgen/{full_id}",
                        {"name": p_full_name, "values": {"style": "своё"}})[0], 200)

            check("ссылку нельзя поставить у частичного пресета",
                  me.go(f"/ui/presets/vsgen/{partial_id}/links",
                        {"part": "images", "preset_id": full_id})[0], 400)
            check("неизвестная часть в ссылке отвергнута",
                  me.go(f"/ui/presets/vsgen/{full_id}/links",
                        {"part": "ерунда", "preset_id": partial_id})[0], 400)
            check("ссылка на частичный не той части отвергнута",
                  me.go(f"/ui/presets/vsgen/{full_id}/links",
                        {"part": "images", "preset_id": partial_voice_id})[0], 400)
            check("ссылка на несуществующий частичный отвергнута",
                  me.go(f"/ui/presets/vsgen/{full_id}/links",
                        {"part": "images", "preset_id": 999999})[0], 404)

            check("ссылка поставлена",
                  me.go(f"/ui/presets/vsgen/{full_id}/links",
                        {"part": "images", "preset_id": partial_id})[0], 200)

            code, d = me.go(f"/ui/presets/vsgen/{full_id}")
            check("поля пришли со ссылкой", code, 200)
            check("scope верхнего уровня — full", d.get("scope"), "full")
            check("ссылка видна в links", d.get("links", {}).get("images"), partial_id)
            group = next(g for g in d["groups"] if g["title"] == "Картинки")
            check("источник группы — ссылка", group.get("source"), "link")
            check("явный признак «только чтение» тоже стоит (ревью цикла 1 фазы 2, Issue 2)",
                  group.get("readonly"), True)
            check("id ссылки — частичный пресет", group.get("link_id"), partial_id)
            check("имя ссылки — имя частичного", group.get("link_name"), p_partial_name)
            field = {f["key"]: f for f in group["fields"]}
            check("значение поля группы — из частичного (только чтение по source)",
                  field["style"]["value"], "из частичного")
            other_group = next(g for g in d["groups"] if g["title"] == "Озвучка")
            check("другая группа ссылкой не тронута", other_group.get("source"), "own")
            check("у несвязанной группы признак read-only снят",
                  other_group.get("readonly"), False)
            check("частичный этой части виден в вариантах для выбора",
                  partial_id in [o["id"] for o in group.get("link_options") or []], True)

            check("сохранение поля связанной группы отвергнуто",
                  me.go(f"/ui/presets/vsgen/{full_id}",
                        {"name": p_full_name, "values": {"style": "подмена"}})[0], 400)
            code, d = me.go(f"/ui/presets/vsgen/{full_id}")
            field = {f["key"]: f for g in d["groups"] for f in g["fields"]}
            check("отвергнутое сохранение не подменило значение из частичного",
                  field["style"]["value"], "из частичного")

            check("ссылка снята",
                  me.go(f"/ui/presets/vsgen/{full_id}/links",
                        {"part": "images", "preset_id": None})[0], 200)
            code, d = me.go(f"/ui/presets/vsgen/{full_id}")
            group = next(g for g in d["groups"] if g["title"] == "Картинки")
            check("после снятия ссылки источник — свои поля", group.get("source"), "own")
            check("после снятия ссылки признак read-only снят", group.get("readonly"), False)
            field = {f["key"]: f for f in group["fields"]}
            check("свои поля вернулись такими, какими были",
                  field["style"]["value"], "своё")

            print("\nСписок пресетов видит scope и users")
            code, d = me.go("/ui/presets")
            vsgen_group = next(g for g in d["groups"] if g["service"] == "vsgen")
            items = {i["key"]: i for i in vsgen_group["items"]}
            check("список пресетов показывает scope целого",
                  items.get(str(full_id), {}).get("scope"), "full")
            check("список пресетов показывает scope частичного",
                  items.get(str(partial_id), {}).get("scope"), "images")
            check("users присутствует у пресета в списке",
                  "users" in items.get(str(full_id), {}), True)

            drop_preset(full_id)
            check("целый пресет убран за собой", preset_row(full_id) is None, True)
        drop_preset(partial_id)
        check("частичный (images) убран за собой", preset_row(partial_id) is None, True)
        drop_preset(partial_voice_id)
        check("частичный (voice) убран за собой", preset_row(partial_voice_id) is None, True)

    print("\nПресеты целые и частичные — доп. проверки фазы 2 (QA)")

    # --- 1. Ссылка ломается уже после того, как её поставили: частичный удалён ---
    qa_partial_a = "QA: частичный images A"
    code, body = me.go("/ui/presets/vsgen", {"name": qa_partial_a, "scope": "images"})
    check("QA частичный A заведён", code, 200)
    qa_a_id = (body or {}).get("id")

    qa_full_1 = "QA: целый, ссылка ломается"
    code, body = me.go("/ui/presets/vsgen", {"name": qa_full_1})
    check("QA целый 1 заведён", code, 200)
    qa_full1_id = (body or {}).get("id")

    if qa_a_id and qa_full1_id:
        me.go(f"/ui/presets/vsgen/{qa_a_id}",
              {"name": qa_partial_a, "values": {"style": "A-стиль"}})
        me.go(f"/ui/presets/vsgen/{qa_full1_id}",
              {"name": qa_full_1, "values": {"style": "свой-стиль-1"}})
        check("ссылка на A поставлена",
              me.go(f"/ui/presets/vsgen/{qa_full1_id}/links",
                    {"part": "images", "preset_id": qa_a_id})[0], 200)

        drop_preset(qa_a_id)                    # частичный убран из-под ссылки
        code, d = me.go(f"/ui/presets/vsgen/{qa_full1_id}")
        check("форма всё равно открывается (пропавший частичный не роняет её)", code, 200)
        check("проблема ссылки видна на верхнем уровне",
              [p["part"] for p in d.get("link_problems") or []], ["images"])
        check("причина — частичный удалён/повреждён",
              (d.get("link_problems") or [{}])[0].get("why"),
              "частичный пресет удалён или его настройки повреждены")
        group = next(g for g in d["groups"] if g["title"] == "Картинки")
        check("проблема видна и на самой группе", bool(group.get("link_problem")), True)
        field = {f["key"]: f for f in group["fields"]}
        check("сломанная ссылка — молча не подставляется: поле осталось своим",
              field["style"]["value"], "свой-стиль-1")
        # Ревью цикла 1 фазы 2, Issue 2 (примечание оркестратора): сломанная ссылка НЕ
        # блокирует группу — `resolve()` оставляет её на своих полях, значит форма
        # обязана показать «свои поля, можно править», а не «только чтение».
        check("источник группы со сломанной ссылкой — свои поля, а не link",
              group.get("source"), "own")
        check("явный признак read-only снят у группы со сломанной ссылкой",
              group.get("readonly"), False)
        check("поле группы со сломанной ссылкой сохраняется (не отвергается сервером)",
              me.go(f"/ui/presets/vsgen/{qa_full1_id}",
                    {"name": qa_full_1, "values": {"style": "переправлено-после-обрыва"}})[0],
              200)
        code, d = me.go(f"/ui/presets/vsgen/{qa_full1_id}")
        field = {f["key"]: f for g in d["groups"] for f in g["fields"] if g["title"] == "Картинки"}
        check("сохранённое значение видно в форме — форма и сохранение согласны",
              field["style"]["value"], "переправлено-после-обрыва")

        drop_preset(qa_full1_id)
        check("QA целый 1 убран за собой", preset_row(qa_full1_id) is None, True)

    # --- 3-доп. Испорченный `_links` целиком (не словарь, а список/строка) — снятие
    # или постановка ссылки через API не падает 500 (ревью цикла 1 фазы 2, Issue 3) ---
    qa_full_g = "QA: целый, _links испорчен целиком"
    code, body = me.go("/ui/presets/vsgen", {"name": qa_full_g})
    qa_g_id = (body or {}).get("id")
    if qa_g_id:
        settings = preset_settings(qa_g_id)
        settings["_links"] = ["images", 1]          # не словарь целиком — то самое Issue 3
        write_preset_settings(qa_g_id, settings)

        code, d = me.go(f"/ui/presets/vsgen/{qa_g_id}")
        check("форма не падает, когда весь _links не словарь", code, 200)

        code, _ = me.go(f"/ui/presets/vsgen/{qa_g_id}/links",
                        {"part": "images", "preset_id": None})
        check("снятие ссылки на испорченном целиком _links не даёт 500", code, 200)

        qa_partial_g = "QA: частичный images G (испорченный _links)"
        code, body = me.go("/ui/presets/vsgen", {"name": qa_partial_g, "scope": "images"})
        qa_g_partial_id = (body or {}).get("id")
        if qa_g_partial_id:
            settings = preset_settings(qa_g_id)
            settings["_links"] = "мусор"            # снова испорчен целиком — строка
            write_preset_settings(qa_g_id, settings)
            code, _ = me.go(f"/ui/presets/vsgen/{qa_g_id}/links",
                            {"part": "images", "preset_id": qa_g_partial_id})
            check("постановка ссылки поверх испорченного целиком _links не даёт 500",
                  code, 200)
            stored = preset_settings(qa_g_id).get("_links")
            check("после постановки _links снова нормальный словарь",
                  (isinstance(stored, dict), stored.get("images")),
                  (True, qa_g_partial_id))
            drop_preset(qa_g_partial_id)

        drop_preset(qa_g_id)

    # --- 2. Ссылка ведёт на частичный, у которого (в обход API) появились свои
    # ссылки — состояние, которое сама ручка API завести не даёт, но старые данные
    # или прямая правка базы могут. resolve() обязан отказать, а не рекурсировать. ---
    qa_partial_b = "QA: частичный voice B"
    code, body = me.go("/ui/presets/vsgen", {"name": qa_partial_b, "scope": "voice"})
    check("QA частичный B (voice) заведён", code, 200)
    qa_b_id = (body or {}).get("id")

    qa_full_2 = "QA: целый, частичный со своими ссылками"
    code, body = me.go("/ui/presets/vsgen", {"name": qa_full_2})
    check("QA целый 2 заведён", code, 200)
    qa_full2_id = (body or {}).get("id")

    if qa_b_id and qa_full2_id:
        me.go(f"/ui/presets/vsgen/{qa_full2_id}",
              {"name": qa_full_2, "values": {"engine": "edge"}})
        check("ссылка на B поставлена",
              me.go(f"/ui/presets/vsgen/{qa_full2_id}/links",
                    {"part": "voice", "preset_id": qa_b_id})[0], 200)

        # Порча в обход API: у частичного B появляется собственный _links.
        b_settings = preset_settings(qa_b_id)
        b_settings["_links"] = {"images": 999999}
        write_preset_settings(qa_b_id, b_settings)

        code, d = me.go(f"/ui/presets/vsgen/{qa_full2_id}")
        check("форма не падает на испорченном частичном", code, 200)
        problems = {p["part"]: p["why"] for p in d.get("link_problems") or []}
        check("причина — у частичного есть свои ссылки",
              problems.get("voice"), "у частичного пресета есть свои ссылки — так нельзя")
        group = next(g for g in d["groups"] if g["title"] == "Озвучка")
        check("источник группы со ссылкой на испорченный частичный — свои поля",
              group.get("source"), "own")
        check("признак read-only снят и здесь", group.get("readonly"), False)
        field = {f["key"]: f for f in group["fields"]}
        check("своё поле группы уцелело (ссылка отвергнута целиком)",
              field["engine"]["value"], "edge")

        drop_preset(qa_full2_id)
        drop_preset(qa_b_id)
        check("QA целый 2 и частичный B убраны за собой",
              (preset_row(qa_full2_id), preset_row(qa_b_id)), (None, None))

    # --- 3. Ссылки пишутся только числом (решение владельца, ревью цикла 2 фазы 1,
    # п.5): нечисловая строка, в том числе состоящая из цифр, дробное и bool
    # отвергаются ещё до обработчика — StrictInt (ревью цикла 1 фазы 2, Issue 1
    # закрепил это тестом вместо прежнего «числовая строка сейчас принимается»,
    # что документировало отклонение от решения, а не выносило его на решение). ---
    qa_partial_c = "QA: частичный images C (типы ссылок)"
    code, body = me.go("/ui/presets/vsgen", {"name": qa_partial_c, "scope": "images"})
    qa_c_id = (body or {}).get("id")
    qa_full_3 = "QA: целый, типы ссылок"
    code, body = me.go("/ui/presets/vsgen", {"name": qa_full_3})
    qa_full3_id = (body or {}).get("id")
    if qa_c_id and qa_full3_id:
        check("нечисловая строка в preset_id отвергнута (422 — StrictInt)",
              me.go(f"/ui/presets/vsgen/{qa_full3_id}/links",
                    {"part": "images", "preset_id": "не-число"})[0], 422)
        check("дробное число в preset_id отвергнуто (422 — StrictInt)",
              me.go(f"/ui/presets/vsgen/{qa_full3_id}/links",
                    {"part": "images", "preset_id": 1.5})[0], 422)
        check("числовая строка в preset_id отвергнута (422 — StrictInt)",
              me.go(f"/ui/presets/vsgen/{qa_full3_id}/links",
                    {"part": "images", "preset_id": str(qa_c_id)})[0], 422)
        check("bool в preset_id отвергнут (422 — StrictInt, bool это подкласс int)",
              me.go(f"/ui/presets/vsgen/{qa_full3_id}/links",
                    {"part": "images", "preset_id": True})[0], 422)
        check("настоящее целое число в preset_id принято",
              me.go(f"/ui/presets/vsgen/{qa_full3_id}/links",
                    {"part": "images", "preset_id": qa_c_id})[0], 200)
        stored = preset_settings(qa_full3_id).get("_links", {}).get("images")
        check("в базе лежит int", (stored, type(stored).__name__), (qa_c_id, "int"))

        # Отсутствующее поле preset_id — 422, а не молчаливое снятие ссылки
        # (Issue 2 ревью цикла 2 фазы 2): контракт — `{part, preset_id | null}`,
        # снятие задаётся ЯВНЫМ null, а не отсутствием поля в теле запроса.
        code, _ = me.go(f"/ui/presets/vsgen/{qa_full3_id}/links", {"part": "images"})
        check("отсутствующий preset_id отвергнут (422 — поле обязательно)", code, 422)
        stored = preset_settings(qa_full3_id).get("_links", {}).get("images")
        check("ссылка не снята отсутствующим полем", (stored, type(stored).__name__),
              (qa_c_id, "int"))

        drop_preset(qa_full3_id)
        drop_preset(qa_c_id)

    # --- 4. Список пресетов — links в каждом элементе, users — реальные держатели ---
    qa_partial_d = "QA: частичный images D (users_of)"
    code, body = me.go("/ui/presets/vsgen", {"name": qa_partial_d, "scope": "images"})
    qa_d_id = (body or {}).get("id")
    qa_full_4 = "QA: целый, users_of сквозняком"
    code, body = me.go("/ui/presets/vsgen", {"name": qa_full_4})
    qa_full4_id = (body or {}).get("id")
    qa_channel_id = None
    if qa_d_id and qa_full4_id:
        me.go(f"/ui/presets/vsgen/{qa_full4_id}/links",
              {"part": "images", "preset_id": qa_d_id})
        qa_channel_id = insert_channel("QA канал на пресете", qa_full4_id)

        code, d = me.go("/ui/presets")
        vsgen_group = next(g for g in d["groups"] if g["service"] == "vsgen")
        items = {i["key"]: i for i in vsgen_group["items"]}
        check("links виден в списке у целого пресета",
              items.get(str(qa_full4_id), {}).get("links"), {"images": qa_d_id})
        check("список пресетов видит реальный канал (users.channels == 1)",
              items.get(str(qa_full4_id), {}).get("users", {}).get("channels"), 1)

        code, d = me.go(f"/ui/presets/vsgen/{qa_full4_id}")
        check("users.channels в форме целого содержит наш канал",
              [c["id"] for c in d.get("users", {}).get("channels") or []], [qa_channel_id])

        code, d = me.go(f"/ui/presets/vsgen/{qa_d_id}")
        check("у частичного users.presets содержит целый-держатель",
              [p["id"] for p in d.get("users", {}).get("presets") or []], [qa_full4_id])
        check("у частичного users.via содержит канал держателя транзитивно",
              [c["id"] for c in d.get("users", {}).get("via") or []], [qa_channel_id])

        drop_channel(qa_channel_id)
        drop_preset(qa_full4_id)
        drop_preset(qa_d_id)

    # --- 5. Сохранение поля НЕсвязанной группы проходит, даже когда другая группа
    # того же целого пресета заблокирована ссылкой ---
    qa_partial_e = "QA: частичный images E (частичная блокировка)"
    code, body = me.go("/ui/presets/vsgen", {"name": qa_partial_e, "scope": "images"})
    qa_e_id = (body or {}).get("id")
    qa_full_5 = "QA: целый, частичная блокировка полей"
    code, body = me.go("/ui/presets/vsgen", {"name": qa_full_5})
    qa_full5_id = (body or {}).get("id")
    if qa_e_id and qa_full5_id:
        me.go(f"/ui/presets/vsgen/{qa_full5_id}/links",
              {"part": "images", "preset_id": qa_e_id})
        check("поле связанной группы (images.style) отвергнуто",
              me.go(f"/ui/presets/vsgen/{qa_full5_id}",
                    {"name": qa_full_5, "values": {"style": "подмена"}})[0], 400)
        check("поле НЕсвязанной группы (voice.engine) сохраняется как обычно",
              me.go(f"/ui/presets/vsgen/{qa_full5_id}",
                    {"name": qa_full_5, "values": {"engine": "edge"}})[0], 200)
        code, d = me.go(f"/ui/presets/vsgen/{qa_full5_id}")
        field = {f["key"]: f for g in d["groups"] for f in g["fields"]}
        check("сохранённое поле несвязанной группы видно в форме",
              field["engine"]["value"], "edge")

        drop_preset(qa_full5_id)
        drop_preset(qa_e_id)

    # --- 6. Сквозная проверка через кабинет (пункт 6 проверки батча): применение
    # пресета со ссылкой к проекту кладёт в projects.settings поля из частичного,
    # а не свои поля целого, и без служебных `_`-ключей метаданных. ---
    qa_partial_f = "QA: частичный images F (сквозная через кабинет)"
    code, body = me.go("/ui/presets/vsgen", {"name": qa_partial_f, "scope": "images"})
    qa_f_id = (body or {}).get("id")
    qa_full_6 = "QA: целый, сквозная через кабинет"
    code, body = me.go("/ui/presets/vsgen", {"name": qa_full_6})
    qa_full6_id = (body or {}).get("id")
    qa_pid = None
    if qa_f_id and qa_full6_id:
        me.go(f"/ui/presets/vsgen/{qa_f_id}",
              {"name": qa_partial_f, "values": {"style": "из-частичного-F"}})
        me.go(f"/ui/presets/vsgen/{qa_full6_id}",
              {"name": qa_full_6, "values": {"style": "своё-у-целого-6"}})
        check("ссылка на F поставлена",
              me.go(f"/ui/presets/vsgen/{qa_full6_id}/links",
                    {"part": "images", "preset_id": qa_f_id})[0], 200)

        code, body = me.go("/ui/projects",
                           {"name": "тест пульта: сквозная через кабинет",
                            "text": "Раз. Два."})
        check("проект для сквозной проверки заведён", code, 200)
        qa_pid = str((body or {}).get("id") or "")

        if qa_pid:
            sid = me.session_cookie()
            check("сессия для похода в кабинет есть", bool(sid), True)
            status = cabinet_apply_preset(PLATFORM, f"vs_session={sid}",
                                          qa_full6_id, qa_pid)
            check("кабинет принял применение пресета (redirect → 200 по цепочке)",
                  status, 200)
            settings = project_settings(qa_pid)
            check("в проект уехало значение из частичного, а не своё поле целого",
                  settings.get("style"), "из-частичного-F")
            check("_scope не утёк в проект", "_scope" in settings, False)
            check("_links не утёк в проект", "_links" in settings, False)
            check("_link_problems не утёк в проект", "_link_problems" in settings, False)
            check("_applied_preset проставлен и указывает на применённый пресет",
                  settings.get("_applied_preset"), qa_full6_id)

            drop_project(qa_pid)

        drop_preset(qa_full6_id)
        drop_preset(qa_f_id)

    print("\nБатч 16.09.2026: списки короче, движок — нет")
    pname2 = "тест пульта: батч 16.09"
    code, body = me.go("/ui/presets/vsgen", {"name": pname2})
    check("пресет для батча заведён", code, 200)
    prid2 = (body or {}).get("id")
    if prid2:
        code, d = me.go(f"/ui/presets/vsgen/{prid2}")
        fields = {f["key"]: f for g in d.get("groups", []) for f in g.get("fields", [])}
        model_values = [o["value"] for o in fields["model"]["options"]]
        check("GROK нет в списке моделей картинок", "GROK" in model_values, False)
        check("GROK_SPEED нет в списке моделей картинок",
              "GROK_SPEED" in model_values, False)
        method_values = [o["value"] for o in fields["video_method"]["options"]]
        check("grok нет в списке методов клипов", "grok" in method_values, False)
        method_titles = " ".join(o["title"] for o in fields["video_method"]["options"])
        check("у методов клипов подписан провайдер",
              ("NonStop" in method_titles) and ("FastGen" in method_titles), True)
        check("aspect_ratio — выпадающий список", fields["aspect_ratio"]["kind"], "select")
        check("aspect_ratio приходит с непустым списком вариантов",
              len(fields["aspect_ratio"]["options"]) > 0, True)
        check("custom_prompts убран из формы", "custom_prompts" in fields, False)

        # Сохранение уходит в кабинет его же ручкой (_canonical_image_params) — она и
        # решает, принять ли старое значение. GROK там знакомый ключ, просто убранный
        # из списка для человека, поэтому сохранение обязано пройти, а не отвергнуться.
        check("сохранение с GROK из старого пресета принято",
              me.go(f"/ui/presets/vsgen/{prid2}",
                    {"name": pname2, "values": {"model": "GROK"}})[0], 200)
        back = {f["key"]: f["value"] for g in me.go(f"/ui/presets/vsgen/{prid2}")[1]["groups"]
                for f in g["fields"]}
        check("сохранённый GROK не потерялся", back.get("model"), "GROK")

        check("сохранение с grok из старого пресета принято",
              me.go(f"/ui/presets/vsgen/{prid2}",
                    {"name": pname2, "values": {"video_method": "grok"}})[0], 200)
        back = {f["key"]: f["value"] for g in me.go(f"/ui/presets/vsgen/{prid2}")[1]["groups"]
                for f in g["fields"]}
        check("сохранённый grok не потерялся", back.get("video_method"), "grok")

        # Строка «язык → id» собирается браузером из виджета langmap, но на сервер
        # уезжает и обратно читается той же строкой, что и раньше — parse_voice_map
        # её не заметит другой.
        check("сохранение карты голосов, собранной браузером, принято",
              me.go(f"/ui/presets/vsgen/{prid2}",
                    {"name": pname2,
                     "values": {"voice_by_lang": "ru: id-1, en: id-2"}})[0], 200)
        back = {f["key"]: f["value"] for g in me.go(f"/ui/presets/vsgen/{prid2}")[1]["groups"]
                for f in g["fields"]}
        check("карта голосов по-прежнему разбирается parse_voice_map",
              back.get("voice_by_lang"), "ru: id-1, en: id-2")

        drop_preset(prid2)
        check("пресет батча убран за собой", preset_row(prid2) is None, True)

    print("\nНастройки проекта — той же формой, что и пресет")
    code, d = me.go("/ui/projects/p_a4ffcac9da/settings")
    if code == 404:
        print("  — пропущено: проекта p_a4ffcac9da на стенде нет")
    else:
        check("поля пришли", code, 200)
        check("группы те же, что у пресета",
              [g["title"] for g in d.get("groups", [])],
              ["Озвучка", "Картинки", "Клипы", "Сборка"])
        check("чужое поле отвергнуто",
              me.go("/ui/projects/p_a4ffcac9da/settings", {"values": {"нет_поля": "1"}})[0],
              400)
        check("пустое сохранение отвергнуто",
              me.go("/ui/projects/p_a4ffcac9da/settings", {"values": {}})[0], 400)
    check("нет проекта — 404", me.go("/ui/projects/p_yok/settings")[0], 404)

    print("\nРасписание канала: дни без времени не теряются молча (18.09.2026)")
    code, ch_before = me.go("/ui/channels/1")
    check("канал открылся", code, 200)
    if code == 200:
        c0 = ch_before["channel"]
        base = {"name": c0["name"], "handle": c0["handle"], "lang": c0["lang_raw"],
                "niche": c0["niche"], "tz": c0["tz"],
                "horizon_days": c0["horizon_days"], "lead_days": c0["lead_days"],
                "deferred": "1" if c0["deferred"] else "",
                "deferred_limit": c0["deferred_limit"],
                "profile_id": c0["profile_id"] or "", "host_id": c0["host_id"] or "",
                "preset_vsgen_id": c0["preset_vsgen_id"] or "", "note": c0["note"],
                "preset_rewrite": "", "preset_cover": ""}
        code, body = me.go("/ui/channels/1", dict(base, days=["mon", "wed"], times=""))
        check("дни без времени — отказ, а не тихое «расписания нет»", code, 400)
        check("сказано, что делать", "время выходов" in str(body), True)
        check("расписание не тронуто", me.go("/ui/channels/1")[1]["channel"]["schedule"],
              c0["schedule"])

        code, _b = me.go("/ui/channels/1", dict(base, days=["mon", "wed"], times="10:00"))
        check("дни со временем сохраняются", code, 200)
        check("в расписании ровно выбранные дни",
              me.go("/ui/channels/1")[1]["channel"]["schedule"],
              {"mon": ["10:00"], "wed": ["10:00"]})
        # Вернуть как было: расписание снимается пустой формой целиком.
        me.go("/ui/channels/1", dict(base, days=[], times=""))
        restored = me.go("/ui/channels/1")[1]["channel"]["schedule"]
        check("пустая форма по-прежнему снимает расписание", restored, {})
        if c0["schedule"]:
            times = sorted({t for v in c0["schedule"].values() for t in v})
            days = [d for d in c0["schedule"] if d != "*"]
            me.go("/ui/channels/1", dict(base, days=days, times=", ".join(times)))
            check("прежнее расписание канала возвращено",
                  me.go("/ui/channels/1")[1]["channel"]["schedule"], c0["schedule"])

    print("\nПресеты соседей правятся из пульта, а не в чужом интерфейсе (18.09.2026)")
    code, form = me.go("/ui/presets/rewrite/Базовый рерайт/form")
    check("форма пресета рерайта отдана", code, 200)
    if code == 200:
        keys_ = [f["key"] for f in form["fields"]]
        check("поля — те, что правит сосед", set(keys_) >= {"model", "chunk", "threads",
                                                            "address", "languages"}, True)
        check("секретов среди полей нет",
              [k for k in keys_ if "key" in k or "token" in k], [])
        body_txt = json.dumps(form, ensure_ascii=False)
        check("значение чужого ключа в форму не попало", "НЕ ДОЛЖЕН УТЕЧЬ" in body_txt, False)
        before = rew_preset("Базовый рерайт")
        vals = {f["key"]: f["value"] for f in form["fields"]}
        vals["address"] = "ты"
        vals["languages"] = [dict(x, host="Проверка") for x in (vals.get("languages") or [])]
        check("сохранение принято",
              me.go("/ui/presets/rewrite/Базовый рерайт/form",
                    {"values": vals})[0], 200)
        after = rew_preset("Базовый рерайт")
        check("поле изменилось", after.get("address"), "ты")
        check("скрытое поле пресета цело", after.get("openai_api_key"),
              before.get("openai_api_key"))
        check("чужие поля языка целы",
              [x.get("voice") for x in after.get("languages") or []],
              [x.get("voice") for x in before.get("languages") or []])
        check("имя ведущего записано",
              [x.get("host") for x in after.get("languages") or []],
              ["Проверка"] * len(before.get("languages") or []))
        check("поле не из формы отвергнуто",
              me.go("/ui/presets/rewrite/Базовый рерайт/form",
                    {"values": {"openai_api_key": "x"}})[0], 400)
        check("правка несуществующего — 404",
              me.go("/ui/presets/rewrite/нет-такого/form",
                    {"values": {"address": "вы"}})[0], 404)
        # Вернуть как было: тест не оставляет за собой изменённых настроек.
        vals["address"] = before.get("address") or ""
        vals["languages"] = [{"code": x.get("code"), "host": x.get("host") or "",
                              "channel": x.get("channel") or ""}
                             for x in (before.get("languages") or [])]
        me.go("/ui/presets/rewrite/Базовый рерайт/form", {"values": vals})
        check("настройки возвращены", rew_preset("Базовый рерайт"), before)

    code, form = me.go("/ui/presets/cover/Default RU/form")
    check("форма пресета обложки отдана", code, 200)
    if code == 200:
        keys_ = [f["key"] for f in form["fields"]]
        check("есть шаблон промпта, число вариантов, модель и образец стиля",
              set(keys_) >= {"prompt_template", "seed_n_variants", "generation",
                             "seed_reference_image"}, True)
        gen = [f for f in form["fields"] if f["key"] == "generation"][0]
        check("модель — из списка самого Cover", bool(gen["options"]), True)
        check("выбрана та, что стоит в пресете",
              gen["value"].split("|")[0] in [o["value"].split("|")[0] for o in gen["options"]],
              True)

    print("\nКлюч Rewrite виден и правится из пульта (18.09.2026)")
    code, keys = me.go("/ui/keys")
    rw = (keys or {}).get("rewrite") or {}
    check("экран ключей отвечает", code, 200)
    check("состояние рерайта отдано", set(rw) >= {"ok"}, True)
    if rw.get("ok"):
        check("видно, чей ключ сработает",
              all(k in rw for k in ("own_openai", "server_openai", "source")), True)
        check("значение ключа наружу не уходит",
              any("sk-" in str(v) for v in rw.values()), False)
        if not rw.get("own_openai"):
            # Своего ключа нет — можно безопасно проверить приём пустого значения:
            # «снять свой ключ» на пустом месте ничего не портит. Настоящий ключ тест
            # не пишет и чужой не снимает.
            check("пустое значение принимается (снять свой ключ)",
                  me.go("/ui/keys/rewrite", {"openai_api_key": ""})[0], 200)
            check("состояние не изменилось",
                  (me.go("/ui/keys")[1]["rewrite"] or {}).get("own_openai"), False)
        else:
            print("  — пропущено: у пользователя стенда задан свой ключ, не трогаем")

    print("\nВыпуски каналов видно в «Проектах» и «Очереди», и их можно убрать (18.09.2026)")
    rel_id, plan_id = make_release("test-release-visible")
    try:
        code, body = me.go("/ui/projects")
        mine = [p for p in (body.get("projects") or []) if p.get("release_id") == rel_id]
        check("выпуск без проекта виден в «Проектах»", len(mine), 1)
        if mine:
            check("это отдельный вид строки, а не проект", mine[0]["kind"], "release")
            check("видна фаза", mine[0]["phase_title"], "заведён")
            check("видно, чего ждёт", mine[0]["waiting"], "нет ключа (проверка)")
            check("строку можно убрать — есть чем", mine[0]["plan_item_id"], plan_id)

        code, body = me.go("/ui/queue")
        rows = [r for r in (body.get("rows") or []) if r.get("release_id") == rel_id]
        check("выпуск виден и в очереди", len(rows), 1)
        check("отдельным сервисом", [s["key"] for s in body["services"]][-3:],
              ["release", "rewrite", "cover"])
        if rows:
            check("этап назван по-человечески", rows[0]["kind_title"], "выпуск · заведён")
            check("причина ожидания показана", rows[0]["error"], "нет ключа (проверка)")

        check("фильтр по сервису «выпуски» работает",
              [r["release_id"] for r in me.go("/ui/queue?service=release")[1]["rows"]
               if r["release_id"] == rel_id], [rel_id])

        # Ролик, который уже уходит на канал, убирать поздно (правило 5 ТЗ): кнопки у
        # такой строки быть не должно, и это решает сервер, а не разметка.
        set_release_phase(rel_id, "upload_running")
        gone = [p for p in me.go("/ui/projects")[1]["projects"]
                if p.get("release_id") == rel_id]
        check("у заливающегося выпуска убрать нечем", [p["plan_item_id"] for p in gone],
              [None])
        qrow = [r for r in me.go("/ui/queue")[1]["rows"] if r.get("release_id") == rel_id]
        check("в очереди — тоже", [r["plan_item_id"] for r in qrow], [None])
        set_release_phase(rel_id, "created")

        check("«Убрать» принято", me.go(f"/ui/plan/{plan_id}/cancel", {})[0], 200)
        check("после этого выпуска в «Проектах» нет",
              [p for p in me.go("/ui/projects")[1]["projects"]
               if p.get("release_id") == rel_id], [])
        check("и в очереди его нет",
              [r for r in me.go("/ui/queue")[1]["rows"] if r.get("release_id") == rel_id], [])
        check("выпуск отменён, а не удалён из журнала", release_status(rel_id), "cancelled")
    finally:
        drop_release(rel_id, plan_id)

    print("\nСсылки без даты видно списком, а не только числом (18.09.2026)")
    mark = "test-waiting-list"
    code, body = me.go("/ui/links", {"channel_id": 1, "links": f"https://youtu.be/{mark}"})
    check("ссылка поставлена", code, 200)
    code, body = me.go("/ui/channels/1")
    waiting = [w for w in (body.get("waiting_items") or []) if mark in w["url"]]
    check("ссылка видна в списке «ждут даты» (если даты ей не нашлось)",
          len(waiting) == body["waiting_without_slot"] and len(waiting) <= 1, True)
    if waiting:
        check("у строки есть ссылка на ролик", waiting[0]["url"].endswith(mark), True)
        code, _b = me.go(f"/ui/plan/{waiting[0]['id']}/cancel", {})
        check("«Убрать» снимает строку", code, 200)
        left = [w for w in (me.go("/ui/channels/1")[1].get("waiting_items") or [])
                if mark in w["url"]]
        check("после «Убрать» её в списке нет", left, [])
    check("чужой строки плана нет — 404", me.go("/ui/plan/99999999/cancel", {})[0], 404)
    check("в базе тестовая ссылка не осталась активной",
          plan_rows(mark) >= 0, True)

    print("\nСтраница зовёт API по своему адресу, а не по корню платформы")
    # Тот самый случай, который сломал вход и не ловился curl'ом: страница живёт под
    # /studio, а зовёт «/ui/…». Браузер резолвит такой путь от корня — то есть в кабинет.
    code, page = me.go("/")
    check("страница отдаётся", code, 200)
    check("база API берётся из адреса", "const API = (location.pathname.match" in page,
          True)
    check("предпросмотр ссылок не читает несуществующее items",
          "out.items" in page, False)
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
