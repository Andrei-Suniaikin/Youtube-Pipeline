#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Фаза заливки доезжает до кабинета целиком и читается им как имя фазы.

Заливщик сообщает фазу машинным маркером — целым JSON по контракту
(`upload/upload_contract.py`: схема, имя фазы, video_id). Пуллер резал его до сорока
символов, и на сервер приезжал огрызок вида `{"phase":"profile_opened","upload_phase_`.
Разобрать такое нельзя, а сравнение на сервере (`phase == "upload_started"`) ждало голого
имени и не совпадало НИКОГДА: фаза «идёт заливка» не попадала в журнал выпуска ни разу,
и движок не мог отличить «файл ещё не отправляли» от «отправка началась». Для заливки это
и есть главный вопрос: повтор до отправки безопасен, после — второй ролик на канале.

Нужен поднятый стенд (как `tests/test_web_pult.py`): проверка идёт по настоящему
эндпоинту, а не по копии его логики.

    VS_PLATFORM_BASE=http://127.0.0.1:8083 VS_DEV_DB=… .venv/bin/python tests/test_upload_phase.py
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "upload"))
sys.path.insert(0, os.path.join(BASE, "server"))

import upload_contract                                            # noqa: E402

PLATFORM = os.environ.get("VS_PLATFORM_BASE", "http://127.0.0.1:8080")
DB = os.environ.get("VS_DEV_DB", os.path.join(BASE, "dev", "vsgen.dev.db"))
TENANT = os.environ.get("VS_DEV_TENANT", "dev")
REL = "r_phase_test"
CLAIM = "claim-phase-test"
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def cleanup(c):
    for sql in ("DELETE FROM pub_upload_jobs WHERE release_id=?",
                "DELETE FROM pub_release_events WHERE release_id=?",
                "DELETE FROM pub_release_log WHERE release_id=?"):
        c.execute(sql, (REL,))
    c.commit()


def _node_token(c):
    """Секрет ноды заливки. Заводится кабинетом при первом обращении — поэтому сначала
    стучимся заведомо чужим токеном (ответ 401 нам и нужен), а потом читаем из базы."""
    row = c.execute("SELECT v FROM admin_state WHERE k='pub_upload_token'").fetchone()
    if row and row[0]:
        return row[0]
    req = urllib.request.Request(f"{PLATFORM}/api/upload/claim", data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Upload-Token", "deliberately-wrong-token")
    try:
        urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError:
        pass
    row = c.execute("SELECT v FROM admin_state WHERE k='pub_upload_token'").fetchone()
    return row[0] if row else ""


def post_phase(token, payload):
    req = urllib.request.Request(
        f"{PLATFORM}/api/upload/{REL}/progress",
        data=json.dumps({"phase": payload}).encode("utf-8"),
        method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Upload-Token", token)
    req.add_header("X-Upload-Claim", CLAIM)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


def main() -> int:
    print("Маркер фазы целиком — и почему обрезка была смертельной")

    line = upload_contract.encode_phase_event(
        upload_contract.advance_phase_event(
            upload_contract.initial_phase_event(), "profile_opened"))
    payload = line[len(upload_contract.PHASE_MARKER):]
    check("маркер длиннее сорока символов", len(payload) > 40, True)
    try:
        json.loads(payload[:40])
        check("обрезанный до 40 символов JSON не читается", "прочитался", "ошибка")
    except ValueError:
        check("обрезанный до 40 символов JSON не читается", "ошибка", "ошибка")

    # Пуллер обязан слать payload целиком: обрезка здесь обесценивает починку на сервере.
    puller = open(os.path.join(BASE, "dev", "upload_puller.py"), encoding="utf-8").read()
    send = re.search(r"say\(rid, claim, phase=([^\n]+)\)", puller)
    check("пуллер найден в исходнике", bool(send), True)
    check("и не режет фазу", "[:40]" in (send.group(1) if send else "[:40]"), False)

    print("\nКабинет разбирает имя фазы и отмечает начатую заливку")

    c = db()
    try:
        token = _node_token(c)
        check("токен ноды заливки есть", bool(token), True)
        cleanup(c)
        now = time.time()
        c.execute(
            "INSERT INTO pub_upload_jobs(release_id,tenant_id,channel_id,uc,channel_url,"
            "title,video_path,slot_at,status,claim_token,created,updated) "
            "VALUES(?,?,1,'UC_test','https://studio.youtube.com/channel/UC_test/',"
            "'Проверка фазы','/dev/null',?,'running',?,?,?)",
            (REL, TENANT, now, CLAIM, now, now))
        c.commit()

        for name in ("preflight", "profile_opened", "upload_started"):
            body = upload_contract.encode_phase_event(
                upload_contract.advance_phase_event(
                    upload_contract.initial_phase_event(), name)
            )[len(upload_contract.PHASE_MARKER):]
            check(f"фаза {name} принята", post_phase(token, body), 200)

        row = c.execute("SELECT phase FROM pub_upload_jobs WHERE release_id=?",
                        (REL,)).fetchone()
        stored = json.loads(row["phase"])
        check("в задании лежит полный, читаемый JSON фазы", stored["phase"], "upload_started")

        events = [r[0] for r in c.execute(
            "SELECT phase FROM pub_release_events WHERE release_id=? ORDER BY seq", (REL,))]
        check("фаза «идёт заливка» записана в журнал выпуска",
              "upload_running" in events, True)
        check("и записана ровно один раз", events.count("upload_running"), 1)

        lines = [r[0] for r in c.execute(
            "SELECT message FROM pub_release_log WHERE release_id=?", (REL,))]
        check("человеку показано имя фазы, а не JSON",
              any(m.endswith("profile_opened") for m in lines), True)
        check("JSON в журнал не попал", any("upload_phase_schema_version" in m
                                            for m in lines), False)

        # Повтор той же фазы не должен плодить вторую отметку.
        body = upload_contract.encode_phase_event(
            upload_contract.advance_phase_event(
                upload_contract.initial_phase_event(), "upload_started")
        )[len(upload_contract.PHASE_MARKER):]
        post_phase(token, body)
        events = [r[0] for r in c.execute(
            "SELECT phase FROM pub_release_events WHERE release_id=?", (REL,))]
        check("повтор фазы вторую отметку не создаёт",
              events.count("upload_running"), 1)
    except urllib.error.URLError as exc:
        print(f"  стенд {PLATFORM} не отвечает: {exc}. Сначала: dev/run.sh")
        FAILS.append("стенд недоступен")
    finally:
        try:
            cleanup(c)
            c.close()
        except Exception:
            pass

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
