#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Озвучка закрепляется при ВЫДАЧЕ задачи рендера, и в tar едет ровно закреплённое.

Дефект: набор файлов озвучки выбирала нода, а не кабинет. Рендерер обходит каталог
проекта рекурсивно и склеивает ВСЁ найденное аудио, сервер же проверял один факт —
«есть хоть один mp3». Значит звук ролика зависел от того, что лежало на диске в момент
рендера: лишняя папка `_FINAL_AUDIO_*` → две озвучки подряд, дозалитый вручную файл →
он же в дорожке, причём после постановки задачи в очередь.

Предохранитель ровно один: на claim сервер выбирает набор
(`render/render_narration.py` — тот же модуль, что стоит на ноде), пишет его в `params`
той же транзакцией, что и claim_token, и отдаёт в материалах ТОЛЬКО эти файлы. Старая
нода про ключ `narration_files` не знает, но «чего нет в архиве — то не склеится»
понимает любая.

Отдельного правила «две папки с mp3 — стоп» в `_render_inputs_ready` больше нет: оно
отвечало иначе, чем модуль, и отвечало МОЛЧА — задача переставала попадать в кандидаты
на выдачу, а причину знал только модуль, до которого дело не доходило. Теперь выбор не
состоялся → задача уходит в `error` с человеческим текстом, и тот же текст показывает
карточка проекта (`_narration_conflict`). Совпадение текстов проверяется здесь же.

Кабинет здесь настоящий, подменяются база, каталог проектов и заглушки про картинки.

    .venv/bin/python tests/test_render_claim_narration.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="render-claim-narration-")

os.environ["VS_SERVER_DISABLE_BACKGROUND"] = "1"
os.environ["VS_SERVER_DB"] = os.path.join(TMP, "vsgen.db")
os.environ["VS_TENANTS_DIR"] = os.path.join(TMP, "tenants")
os.environ["PULLER_TOKEN"] = "test-puller-token"
os.environ.setdefault("HOME", os.path.join(BASE, "dev", "home"))
sys.path.insert(0, os.path.join(BASE, "server"))
sys.path.insert(0, os.path.join(BASE, "workers"))

import main as M                                                  # noqa: E402
from starlette.testclient import TestClient                       # noqa: E402

FAILS = []
TID, PID = "dev", "proj-claim-narration"
TOKEN = os.environ["PULLER_TOKEN"]
SETTINGS = {}


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def projdir():
    return M._project_dir(TID, PID)


def audio(rel, size=2048):
    path = os.path.join(projdir(), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"a" * size)


def project(audios, images=3):
    """Чистый проект: картинки для `_materials_ready` плюс перечисленные файлы озвучки."""
    shutil.rmtree(projdir(), ignore_errors=True)
    os.makedirs(os.path.join(projdir(), "googler"), exist_ok=True)
    for i in range(images):
        open(os.path.join(projdir(), "googler", f"{i + 1:03d}.jpg"), "wb").write(b"x" * 10)
    for rel in audios:
        audio(rel)


def task(jid, params="{}"):
    """Свежая очередь проекта: claim берёт СТАРЕЙШУЮ готовую задачу, и хвост от
    предыдущего сценария увёл бы проверку не на ту строку."""
    c = M.db()
    c.execute("DELETE FROM queue_tasks WHERE tenant_id=? AND project_id=?", (TID, PID))
    c.execute("INSERT INTO queue_tasks(job_id,tenant_id,project_id,kind,status,created,params,progress)"
              " VALUES(?,?,?,'render','queued',?,?,0)", (jid, TID, PID, time.time(), params))
    c.commit()
    c.close()


def row(jid):
    c = M.db()
    r = c.execute("SELECT * FROM queue_tasks WHERE job_id=?", (jid,)).fetchone()
    c.close()
    return r


def params_of(jid):
    return json.loads(row(jid)["params"] or "{}")


def claim(client):
    return client.post("/api/render/claim", headers={"X-Puller-Token": TOKEN}, json={}).json()


def expire(jid):
    """Аренда протухла — задача вернётся в очередь на следующем опросе (перезахват)."""
    c = M.db()
    c.execute("UPDATE queue_tasks SET claimed=? WHERE job_id=?",
              (time.time() - M.RENDER_LEASE_SEC - 60, jid))
    c.commit()
    c.close()


def tar_names(client, jid, claim_token):
    r = client.get(f"/api/render/{jid}/assets",
                   headers={"X-Puller-Token": TOKEN, "X-Render-Claim": claim_token})
    if r.status_code != 200:
        return r.status_code
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
        return sorted(m.name for m in tf.getmembers() if m.isfile())


def audio_in_tar(client, jid, claim_token):
    names = tar_names(client, jid, claim_token)
    if not isinstance(names, list):
        return names
    return [n for n in names if os.path.splitext(n)[1].lower() in (".mp3", ".wav")]


def running_task(jid, params, claim_token):
    """Строка уже выданной задачи — для случаев, которые через claim не воспроизвести."""
    c = M.db()
    c.execute("DELETE FROM queue_tasks WHERE tenant_id=? AND project_id=?", (TID, PID))
    c.execute("INSERT INTO queue_tasks(job_id,tenant_id,project_id,kind,status,created,params,"
              "claim_token,claimed,progress) VALUES(?,?,?,'render','running',?,?,?,?,0)",
              (jid, TID, PID, time.time(), params, claim_token, time.time()))
    c.commit()
    c.close()


def main() -> int:
    client = TestClient(M.app)
    saved = (M._get_project, M._psettings, M._images_ratio_ok, M._images_gap_ok)
    M._get_project = lambda tid, pid: {"name": "тест", "settings": "{}"}
    M._psettings = lambda p: dict(SETTINGS)
    M._images_ratio_ok = lambda tid, pid: True
    M._images_gap_ok = lambda tid, pid, st=None: True
    try:
        print("Обычный проект: набор закрепляется и уезжает в конфиг")

        SETTINGS.clear(); SETTINGS.update({"engine": "edge"})
        project(["_FINAL_AUDIO_EDGETTS/001.mp3", "_FINAL_AUDIO_EDGETTS/002.mp3"])
        task(1)
        got = claim(client)
        check("задача выдана", bool(got.get("job")), True)
        want = ["_FINAL_AUDIO_EDGETTS/001.mp3", "_FINAL_AUDIO_EDGETTS/002.mp3"]
        check("набор закреплён в params", params_of(1).get("narration_files"), want)
        check("и уехал в конфиг ноды", got["job"]["config"].get("narration_files"), want)
        check("задача в работе", row(1)["status"], "running")
        # Снимок пишется ТОЙ ЖЕ транзакцией, что и claim_token: иначе перезахват и
        # закрепление могли бы разойтись, и в логе стоял бы один набор, а в ролике другой.
        check("claim_token выдан вместе со снимком", bool(row(1)["claim_token"]), True)

        print("\nФайл, появившийся после выдачи задачи, в ролик не попадает")

        first_token = got["job"]["claim_token"]
        audio("_FINAL_AUDIO_EDGETTS/003.mp3")       # ручная дозаливка во время рендера
        check("закрепление не поехало", params_of(1).get("narration_files"), want)
        check("в tar только закреплённое аудио", audio_in_tar(client, 1, first_token), want)

        print("\nПерезахват пишет СВОЙ снимок, а не наследует старый")

        expire(1)
        got2 = claim(client)
        check("задача выдана снова", bool(got2.get("job")), True)
        check("новый claim_token", got2["job"]["claim_token"] != first_token, True)
        want2 = want + ["_FINAL_AUDIO_EDGETTS/003.mp3"]
        check("снимок пересчитан по диску", params_of(1).get("narration_files"), want2)
        check("и tar поехал за ним", audio_in_tar(client, 1, got2["job"]["claim_token"]), want2)

        print("\nЗакреплённый файл исчез с диска — материалы не отдаются, а объясняют")

        # Отдать архив с ЧАСТЬЮ озвучки — худший исход: ролик соберётся, будет выглядеть
        # готовым и молча потеряет кусок текста. Поэтому отказ, а не «что нашлось».
        os.remove(os.path.join(projdir(), "_FINAL_AUDIO_EDGETTS/002.mp3"))
        check("материалы не отданы", tar_names(client, 1, got2["job"]["claim_token"]), 409)

        print("\nПапка не того движка: задача уходит в ошибку с человеческим текстом")

        # Движок в проекте не тот, которым озвучено: сегодня такой проект тихо
        # рендерился чужим голосом. Папка при этом ОДНА — прежнему правилу «папок
        # больше одной» ловить тут было нечего.
        SETTINGS.clear(); SETTINGS.update({"engine": "voicer"})
        project(["_FINAL_AUDIO_EDGETTS/001.mp3"])
        task(2)
        check("папка ровно одна", len(M._narration_dirs(projdir())), 1)
        got = claim(client)
        check("задача не выдана", got.get("job"), None)
        check("задача не висит в очереди молча", row(2)["status"], "error")
        check("причина по-русски", "выбранного движка" in (row(2)["error"] or ""), True)
        # Один источник правды: карточка проекта показывает ровно ту же причину, что
        # записана в задачу, — не похожую, а начинающуюся теми же словами.
        check("плашка проекта говорит то же самое",
              M._narration_conflict(TID, PID, dict(SETTINGS)).startswith(row(2)["error"]), True)

        print("\nВторая папка с wav: стоп её не считает, закрепление — считает")

        # `_narration_dirs` смотрит только `*.mp3`, а рендерер склеивает и wav. Ровно эта
        # щель и даёт задвоенный звук при живом стопе.
        SETTINGS.clear(); SETTINGS.update({"engine": "lumean"})
        project(["_FINAL_AUDIO_LUMEAN/001.mp3", "_FINAL_AUDIO_EDGETTS/001.wav"])
        task(3)
        check("движок выбран — возражений нет",
              M._narration_conflict(TID, PID, dict(SETTINGS)), "")
        got = claim(client)
        check("задача выдана", bool(got.get("job")), True)
        check("закреплена только папка выбранного движка",
              params_of(3).get("narration_files"), ["_FINAL_AUDIO_LUMEAN/001.mp3"])
        check("чужой wav в материалы не поехал",
              audio_in_tar(client, 3, got["job"]["claim_token"]),
              ["_FINAL_AUDIO_LUMEAN/001.mp3"])

        print("\nТа же щель без выбранного движка — отказ словами, а не «нет задач»")

        SETTINGS.clear()
        project(["_FINAL_AUDIO_LUMEAN/001.mp3", "_FINAL_AUDIO_EDGETTS/001.wav"])
        task(4)
        check("задача не выдана", claim(client).get("job"), None)
        check("задача в ошибке", row(4)["status"], "error")
        check("сказано, что вариантов несколько",
              "есколько вариантов" in (row(4)["error"] or ""), True)
        check("и плашка проекта говорит то же самое",
              M._narration_conflict(TID, PID, {}).startswith(row(4)["error"]), True)

        print("\nДве папки с mp3 при выбранном движке: рендер ИДЁТ, а не висит молча")

        # Раньше здесь срабатывал стоп `_narration_conflict` в `_render_inputs_ready`:
        # задача не выдавалась и не ошибалась — просто исчезала из кандидатов. Теперь
        # решает модуль: он берёт папку выбранного движка, а в tar уезжает только она,
        # поэтому склеить две дорожки ноде физически не из чего.
        SETTINGS.clear(); SETTINGS.update({"engine": "lumean"})
        project(["_FINAL_AUDIO_LUMEAN/001.mp3", "_FINAL_AUDIO_EDGETTS/001.mp3"])
        task(5)
        check("гейт готовности озвучку не судит", M._render_inputs_ready(TID, PID), True)
        got = claim(client)
        check("задача выдана", bool(got.get("job")), True)
        check("закреплена только папка выбранного движка",
              params_of(5).get("narration_files"), ["_FINAL_AUDIO_LUMEAN/001.mp3"])
        check("чужая папка в материалы не поехала",
              audio_in_tar(client, 5, got["job"]["claim_token"]),
              ["_FINAL_AUDIO_LUMEAN/001.mp3"])

        print("\nТе же две папки, но движок НЕ выбран — ошибка словами, а не тишина")

        SETTINGS.clear()
        project(["_FINAL_AUDIO_LUMEAN/001.mp3", "_FINAL_AUDIO_EDGETTS/001.mp3"])
        task(9)
        check("задача не выдана", claim(client).get("job"), None)
        check("и не осталась висеть в очереди", row(9)["status"], "error")
        check("причина названа", "есколько вариантов" in (row(9)["error"] or ""), True)

        print("\nОзвучка нулевого размера: «файл есть» и «озвучка есть» — разные вещи")

        # `_materials_ready` смотрит только на существование mp3 по glob'у, размер его не
        # интересует. Оборвавшийся на записи TTS оставляет пустой файл — до этой правки
        # такой проект уезжал на ноду и возвращался немым роликом или падением рендерера.
        SETTINGS.clear(); SETTINGS.update({"engine": "edge"})
        project([])
        audio("_FINAL_AUDIO_EDGETTS/001.mp3", size=0)
        ready, _n, _a = M._materials_ready(projdir())
        check("материалы кабинет считает готовыми", ready, True)
        task(8)
        check("задача не выдана", claim(client).get("job"), None)
        check("задача в ошибке", row(8)["status"], "error")
        check("сказано, что файлов озвучки нет",
              "Нет файлов основной озвучки" in (row(8)["error"] or ""), True)

        print("\nОзвучка внутри Images/ кладётся в архив один раз, а не дважды")

        # Такой проект приезжает из старых времён: озвучка лежит не в `_FINAL_AUDIO_*`.
        # Папка Images и так едет целиком, поэтому второй член архива на тот же файл —
        # это лишние мегабайты на каждой заливке материалов.
        project(["Images/voice.mp3"])
        running_task(7, json.dumps({"narration_files": ["Images/voice.mp3"]}), "img-claim")
        check("файл в архиве ровно один раз", audio_in_tar(client, 7, "img-claim"),
              ["Images/voice.mp3"])

        print("\nСтарая задача без закрепления: материалы едут по-прежнему, целыми папками")

        SETTINGS.clear(); SETTINGS.update({"engine": "edge"})
        project(["_FINAL_AUDIO_EDGETTS/001.mp3", "_FINAL_AUDIO_LUMEAN/001.mp3"])
        running_task(6, "{}", "old-claim")
        check("закрепления нет", params_of(6).get("narration_files"), None)
        check("значит, везём всё, как до правки",
              audio_in_tar(client, 6, "old-claim"),
              ["_FINAL_AUDIO_EDGETTS/001.mp3", "_FINAL_AUDIO_LUMEAN/001.mp3"])
    finally:
        (M._get_project, M._psettings, M._images_ratio_ok, M._images_gap_ok) = saved
        client.close()
        shutil.rmtree(TMP, ignore_errors=True)

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
