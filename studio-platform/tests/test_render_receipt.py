#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Рендер принимается по квитанции: принят ровно тот файл, который нода и отдавала.

Было так: тело POST /api/render/{jid}/result писалось ПРЯМО в `render/{jid}.mp4`,
проверка одна — «больше 1024 байт», ответ — `{"ok": true}`. Отсюда два дефекта.

1. Оборванная заливка оставляла обрезанный mp4 под штатным именем, задача вставала в
   `done`, и на YouTube уезжал огрызок: тело сырое, конца у потока нет, отличить
   «доехало целиком» от «оборвалось на 90%» было нечем.
2. Боевая нода (файлы с 14.09) говорит по протоколу квитанции: зовёт
   `GET /api/render/{jid}/receipt` и разбирает ответ `POST …/result` как квитанцию
   (`accepted/job_id/status/sha256/size`). Ручки у нас не было (404), ответ квитанцией
   не был — нода считала доставку несостоявшейся и перезаливала готовый ролик до
   двадцати раз, после чего уходила в карантин, держа гигабайты на своём диске.

Теперь: тело во временный `.incoming-*` с fsync → сверка Content-Length → сверка
`X-Render-SHA256` → атомарный `os.replace` → запись sha и размера в строку задачи.
Повтор той же доставки отдаёт ту же квитанцию, другой результат на ту же задачу — 409.
Заголовков нет вовсе (наш стендовый пуллер) — приём работает как раньше.

Кабинет здесь настоящий, подменяются только база и каталог проектов — на временные.

    .venv/bin/python tests/test_render_receipt.py
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="render-receipt-")

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
TID, PID = "dev", "proj-receipt"
TOKEN = os.environ["PULLER_TOKEN"]
CLAIM = "claim-token-first"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"video-bytes" * 200          # заведомо больше 1024


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def projdir() -> str:
    return M._project_dir(TID, PID)


def render_dir() -> str:
    return os.path.join(projdir(), "render")


def incoming() -> list:
    """Недокачанные тела: их не должно оставаться ни после успеха, ни после отказа."""
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(render_dir(), ".incoming-*")))


def task(jid, status="running", claim=CLAIM, params="{}", **cols):
    """Строка render-задачи в нужном состоянии (клиент кабинета её не создаёт: задачу
    ставит очередь, а тест проверяет именно приём результата)."""
    c = M.db()
    c.execute("DELETE FROM queue_tasks WHERE job_id=?", (jid,))
    c.execute("INSERT INTO queue_tasks(job_id,tenant_id,project_id,kind,status,created,claim_token,params,progress)"
              " VALUES(?,?,?,'render',?,?,?,?,0)",
              (jid, TID, PID, status, time.time(), claim, params))
    for k, v in cols.items():
        c.execute(f"UPDATE queue_tasks SET {k}=? WHERE job_id=?", (v, jid))
    c.commit()
    c.close()


def row(jid):
    c = M.db()
    r = c.execute("SELECT * FROM queue_tasks WHERE job_id=?", (jid,)).fetchone()
    c.close()
    return r


def post(client, jid, body=b"", claim=CLAIM, status="done", headers=None, params=None):
    h = {"X-Puller-Token": TOKEN, "X-Render-Claim": claim, "Content-Type": "video/mp4"}
    h.update(headers or {})
    q = {"status": status}
    q.update(params or {})
    return client.post(f"/api/render/{jid}/result", params=q, headers=h, content=body)


def receipt(client, jid, claim=CLAIM):
    return client.get(f"/api/render/{jid}/receipt",
                      headers={"X-Puller-Token": TOKEN, "X-Render-Claim": claim})


def main() -> int:
    os.makedirs(render_dir(), exist_ok=True)
    client = TestClient(M.app)

    print("Успешная доставка: файл на месте, квитанция сходится с диском")

    task(1)
    r = post(client, 1, MP4, headers={"X-Render-SHA256": sha(MP4), "Content-Length": str(len(MP4))})
    check("приём подтверждён", r.status_code, 200)
    body = r.json() if r.status_code == 200 else {}
    check("нода видит accepted", body.get("accepted"), True)
    check("номер задачи в квитанции", body.get("job_id"), 1)
    check("sha256 посчитан по телу", body.get("sha256"), sha(MP4))
    check("размер в квитанции", body.get("size"), len(MP4))
    t = row(1)
    check("задача закрыта", t["status"], "done")
    check("путь записан", t["output_rel"], "render/1.mp4")
    check("sha записан в строку", t["render_sha256"], sha(MP4))
    check("размер записан в строку", t["render_size"], len(MP4))
    check("на диске ровно то тело",
          open(os.path.join(projdir(), "render", "1.mp4"), "rb").read(), MP4)
    check("временных .incoming-* не осталось", incoming(), [])

    print("\nКвитанция считается по диску, а не по базе")

    r = receipt(client, 1)
    check("GET /receipt повторяет ту же квитанцию", (r.status_code, r.json().get("sha256")),
          (200, sha(MP4)))
    os.remove(os.path.join(projdir(), "render", "1.mp4"))
    r = receipt(client, 1)
    check("файл пропал с диска — 503 «держи копию», а не подтверждение", r.status_code, 503)

    print("\nContent-Length и X-Render-SHA256 — это и есть проверка целостности")

    task(2)
    r = post(client, 2, MP4, headers={"Content-Length": str(len(MP4) + 10)})
    check("объявленная длина не сошлась — отказ", r.status_code, 400)
    check("файла в render/ не появилось", os.path.exists(os.path.join(render_dir(), "2.mp4")), False)
    check("временное тело убрано", incoming(), [])
    check("задача осталась в работе (нода повторит)", row(2)["status"], "running")

    r = post(client, 2, MP4, headers={"X-Render-SHA256": sha("другой ролик".encode("utf-8"))})
    check("sha не сошёлся — отказ", r.status_code, 400)
    check("и файла по-прежнему нет", os.path.exists(os.path.join(render_dir(), "2.mp4")), False)
    check("временное тело убрано и тут", incoming(), [])

    r = post(client, 2, b"crash", headers={"X-Render-SHA256": sha(b"crash")})
    check("тело меньше 1024 байт — отказ", r.status_code, 400)
    check("огрызок не сохранён", os.path.exists(os.path.join(render_dir(), "2.mp4")), False)

    saved_max = M.RENDER_MAX_BYTES
    M.RENDER_MAX_BYTES = 4096
    try:
        r = post(client, 2, b"x" * 8192)
        check("тело больше потолка — 413", r.status_code, 413)
    finally:
        M.RENDER_MAX_BYTES = saved_max
    check("недокачанное тело удалено", incoming(), [])

    print("\nПовтор той же доставки идемпотентен, другой результат — отказ")

    task(3)
    first = post(client, 3, MP4, headers={"X-Render-SHA256": sha(MP4)})
    again = post(client, 3, MP4, headers={"X-Render-SHA256": sha(MP4)})
    check("повтор принят", again.status_code, 200)
    check("квитанция та же самая", again.json(), first.json())
    check("второго файла не завелось",
          sorted(os.path.basename(p) for p in glob.glob(os.path.join(render_dir(), "*.mp4"))),
          ["3.mp4"])

    other = "совсем другой ролик".encode("utf-8") * 100
    r = post(client, 3, other, headers={"X-Render-SHA256": sha(other)})
    check("другой результат на ту же задачу — 409", r.status_code, 409)
    check("принятый ролик не подменён",
          open(os.path.join(render_dir(), "3.mp4"), "rb").read(), MP4)

    print("\nЧужая аренда не трогает принятый файл")

    task(4)
    post(client, 4, MP4, headers={"X-Render-SHA256": sha(MP4)})
    r = post(client, 4, other, claim="claim-token-revoked",
             headers={"X-Render-SHA256": sha(other)})
    check("устаревший X-Render-Claim — 409", r.status_code, 409)
    check("файл первой ноды цел", open(os.path.join(render_dir(), "4.mp4"), "rb").read(), MP4)
    check("мусора не осталось", incoming(), [])

    print("\nЗадача ещё в работе — «я тебя не приняла, заливай»")

    task(5)
    r = receipt(client, 5)
    check("running → accepted:false", (r.status_code, r.json().get("accepted")), (200, False))
    check("статус назван прямо", r.json().get("status"), "running")
    r = receipt(client, 5, claim="claim-token-alien")
    check("чужая аренда к квитанции не допущена", r.status_code, 409)
    check("без токена пуллера ручки нет",
          client.get("/api/render/5/receipt").status_code, 403)

    print("\nОшибка рендера тоже принимается по хэшу")

    text = "render rc=1, ffmpeg: no such file"
    r = post(client, 5, status="error", params={"error": text},
             headers={"X-Render-Error-SHA256": sha(b"not-that-error-at-all")})
    check("хэш ошибки не сошёлся — 400", r.status_code, 400)
    check("задача не закрыта чужой ошибкой", row(5)["status"], "running")

    r = post(client, 5, status="error", params={"error": text},
             headers={"X-Render-Error-SHA256": sha(text.encode("utf-8"))})
    check("верный хэш — ошибка принята", r.status_code, 200)
    check("в квитанции хэш ошибки", r.json().get("error_sha256"), sha(text.encode("utf-8")))
    check("текст ошибки сохранён", row(5)["error"], text)
    check("повтор той же ошибки идемпотентен",
          post(client, 5, status="error", params={"error": text},
               headers={"X-Render-Error-SHA256": sha(text.encode("utf-8"))}).status_code, 200)

    print("\nНаш стендовый пуллер заголовков не шлёт — и это работает")

    task(6)
    r = client.post("/api/render/6/result", params={"status": "done"},
                    headers={"X-Puller-Token": TOKEN, "X-Render-Claim": CLAIM},
                    content=MP4)
    check("приём без заголовков", r.status_code, 200)
    check("sha всё равно посчитан сервером", row(6)["render_sha256"], sha(MP4))
    check("квитанция полная", r.json().get("accepted"), True)

    print("\nСтендовый пуллер и сервер сходятся в заголовках")

    # Проверяем не «пуллер что-то шлёт», а что ИМЕННО то, что он шлёт, сервер принимает:
    # заголовки считает пуллер, а сверяет сервер, и разъехаться они могут молча.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "render_puller_stand", os.path.join(BASE, "dev", "render_puller.py"))
    puller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(puller)

    sent = {}

    class _Answer:
        status_code = 200

        def json(self):
            return {"ok": True, "accepted": True, "job_id": 8, "status": "done",
                    "sha256": sha(MP4), "size": len(MP4)}

        def raise_for_status(self):
            return None

    def _capture(url, headers=None, data=None, **kw):
        sent.update(url=url, headers=headers or {}, body=data.read() if data else b"")
        return _Answer()

    saved_post = puller.requests.post
    puller.requests.post = _capture
    try:
        mp4 = os.path.join(TMP, "stand.mp4")
        open(mp4, "wb").write(MP4)
        puller.post_done(8, CLAIM, mp4)
    finally:
        puller.requests.post = saved_post
    check("пуллер объявляет длину", sent["headers"].get("Content-Length"), str(len(MP4)))
    check("пуллер считает sha сам", sent["headers"].get("X-Render-SHA256"), sha(MP4))

    task(8)
    r = post(client, 8, sent["body"], headers={k: v for k, v in sent["headers"].items()
                                               if k.startswith("X-Render") or k == "Content-Length"})
    check("сервер принимает ровно эти заголовки", r.status_code, 200)
    check("и подтверждает тот же файл", r.json().get("sha256"), sha(MP4))

    print("\nСтарые рендеры (квитанции нет) продолжают открываться и заливаться")

    sys.path.insert(0, os.path.join(BASE, "server"))
    from pub_publish import find_video                            # noqa: E402

    legacy = os.path.join(render_dir(), "7.mp4")
    open(legacy, "wb").write(MP4)
    task(7, status="done", output_rel="render/7.mp4", claim="claim-token-legacy")
    check("колонки квитанции у старой строки пусты",
          (row(7)["render_sha256"], row(7)["render_size"]), (None, None))
    check("заливка находит файл по output_rel",
          find_video(projdir(), 7, row(7)["output_rel"]), legacy)
    check("и по штатному имени, если output_rel потеряли",
          find_video(projdir(), 7, ""), legacy)
    r = receipt(client, 7, claim="claim-token-legacy")
    check("квитанции по старому рендеру нет — 503, а не выдумка", r.status_code, 503)
    r = post(client, 7, other, claim="claim-token-legacy", headers={"X-Render-SHA256": sha(other)})
    check("дослать другой результат в старую задачу нельзя", r.status_code, 409)
    check("старый ролик не тронут", open(legacy, "rb").read(), MP4)

    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
