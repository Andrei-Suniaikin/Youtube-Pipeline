#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пуллер заливки для минской машины: берёт задание у кабинета и льёт ролик на канал.

Устроен как рендер-нода: только исходящие запросы, никаких открытых портов. Отличия
продиктованы тем, что заливка необратима:

* **берём по одному заданию на рабочее место.** Сервер сам не выдаст два задания на один
  профиль, но и мы не запускаем в профиле второй браузер;
* **о начале заливки сообщаем ДО того, как она началась.** Сервер по этому сообщению
  запрещает повтор навсегда. Если мы упадём в момент загрузки, ролик не будет залит второй раз;
* **вывод заливщика уезжает на сервер построчно.** После сбоя видно, что именно случилось,
  а не «нода молчит»;
* **ничего не удаляем сами.** Файлы кладём в рабочую папку и чистим только после вердикта.

Запуск: см. юнит vsupload-puller.service. Интерпретатор обязателен из `.venv-upload`,
в системном питоне Минска нет selenium.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

SERVER = os.environ.get("VSGEN_SERVER", "https://gen.13.140.129.66.sslip.io").rstrip("/")
TOKEN = os.environ.get("UPLOAD_TOKEN", "")
HOST = os.environ.get("UPLOAD_HOST", "minsk")
WORKER = os.environ.get("UPLOAD_WORKER_SLOT", "1")
WORK = os.environ.get("UPLOAD_WORK", "/home/vova/upload_jobs")
VERDICTS = os.path.join(WORK, "_verdicts")
UPLOADER = os.environ.get("UPLOAD_WORKER_PY", "/home/vova/VideoStudio/src/upload_worker.py")
POLL_SEC = int(os.environ.get("UPLOAD_POLL_SEC", "20"))
HTTP_TIMEOUT = int(os.environ.get("UPLOAD_HTTP_TIMEOUT", "60"))
UPLOAD_DEADLINE_SEC = int(os.environ.get("UPLOAD_DEADLINE_SEC", "2400"))

RESULT_MARK = "__UPLOAD_RESULT__|"
PHASE_MARK = "__UPLOAD_PHASE__|"


def log(msg: str) -> None:
    print(f"[upload-puller] {msg}", flush=True)


def call(path: str, payload=None, claim: str = "", raw: bool = False, method: str = ""):
    url = f"{SERVER}{path}"
    data = None
    headers = {"X-Upload-Token": TOKEN}
    if claim:
        headers["X-Upload-Claim"] = claim
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                method=method or ("POST" if data is not None else "GET"))
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        body = resp.read()
    return body if raw else json.loads(body.decode("utf-8") or "{}")


def download(release_id: str, kind: str, claim: str, dest: str) -> str:
    """Привезти файл с сервера: минская папка рендера чистится, ролик живёт на VPS."""
    url = f"{SERVER}/api/upload/{release_id}/file?kind={kind}"
    req = urllib.request.Request(url, headers={"X-Upload-Token": TOKEN,
                                              "X-Upload-Claim": claim})
    tmp = dest + ".part"
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT * 10) as resp, \
                open(tmp, "wb") as out:
            # Качаем кусками и каждые ~60 секунд подаём голос: иначе большой ролик на
            # медленном канале молчит дольше аренды, сервер снимает задание в разбор,
            # а нода после скачивания как ни в чём не бывало пошла бы заливать.
            done, last_beat = 0, time.time()
            while True:
                chunk = resp.read(1024 * 512)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if time.time() - last_beat > 60:
                    say(release_id, claim,
                        stdout=f"качаю {kind}: {done // (1024 * 1024)} МБ")
                    last_beat = time.time()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return ""              # обложки может не быть — это не ошибка
        raise
    os.replace(tmp, dest)
    return dest


def say(release_id: str, claim: str, phase: str = "", stdout: str = "") -> None:
    """Сообщить серверу фазу и вывод. Молчание дороже: по нему нас снимут по сроку.

    Фазы повторяем до пяти раз: по фазе «началась загрузка» сервер навсегда запрещает
    повтор, и потерять её из-за одного сетевого чиха нельзя. Не доставили — заливка всё
    равно закончится вердиктом, а недоехавшая фаза оставит задание в разборе у человека,
    что безопасно; но стараемся доставить.
    """
    attempts = 5 if phase else 1
    for i in range(attempts):
        try:
            call(f"/api/upload/{release_id}/progress",
                 {"phase": phase, "stdout": stdout}, claim=claim)
            return
        except Exception as exc:
            if i == attempts - 1:
                log(f"не смог сообщить фазу «{phase}»: {exc}")
            else:
                time.sleep(3 * (i + 1))


def run_upload(task: dict, projdir: str, video: str, cover: str) -> dict:
    """Запустить заливщик и вернуть его разобранный результат."""
    argv = [sys.executable, UPLOADER,
            "--video", video,
            "--title", task["title"],
            "--profile", str(task["profile"]),
            "--channel-url", task["channel_url"]]
    if task.get("description"):
        argv += ["--description", task["description"]]
    if cover:
        argv += ["--thumbnail", cover]
    if task.get("schedule"):
        argv += ["--schedule", task["schedule"]]
    else:
        argv += ["--visibility", task.get("visibility") or "public"]
    if task.get("collaborator"):
        argv += ["--collaborator", task["collaborator"]]

    rid, claim = task["release_id"], task["claim_token"]
    log(f"{rid}: запускаю заливщик, профиль {task['profile']}")
    proc = subprocess.Popen(argv, cwd=os.path.dirname(UPLOADER) or ".",
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    # Читаем вывод отдельным потоком: заливщик может замолчать, оставаясь живым
    # (больной прокси), а зависший подпроцесс не должен держать место вечно.
    import queue as _q
    import threading as _th
    lines: _q.Queue = _q.Queue()

    def _reader():
        for raw in proc.stdout:
            lines.put(raw.rstrip())
        lines.put(None)

    _th.Thread(target=_reader, daemon=True).start()
    result = None
    buffer, last_beat, started = [], time.time(), time.time()
    while True:
        if time.time() - started > UPLOAD_DEADLINE_SEC:
            proc.kill()
            say(rid, claim, stdout=f"заливщик превысил срок {UPLOAD_DEADLINE_SEC // 60} мин — снят")
            if result is None:
                result = {"success": False,
                          "error": f"заливщик молчал дольше {UPLOAD_DEADLINE_SEC // 60} минут"}
            break
        try:
            line = lines.get(timeout=5)
        except _q.Empty:
            continue
        if line is None:
            break
        if not line:
            continue
        buffer.append(line)
        if line.startswith(RESULT_MARK):
            try:
                result = json.loads(line[len(RESULT_MARK):])
            except Exception:
                result = {"success": False, "error": "не разобрал результат заливщика"}
        if line.startswith(PHASE_MARK):
            # Фазу отправляем немедленно: сервер по ней запрещает повтор.
            say(rid, claim, phase=line[len(PHASE_MARK):].strip()[:40])
        if time.time() - last_beat > 60:
            say(rid, claim, stdout="\n".join(buffer[-30:]))
            buffer, last_beat = [], time.time()
    proc.wait()
    tail = "\n".join(buffer[-60:])
    if result is None:
        result = {"success": False,
                  "error": f"заливщик завершился с кодом {proc.returncode} без результата"}
    return {"result": result, "stdout": tail}


def deliver(rid: str, claim: str, out: dict) -> bool:
    """Довезти вердикт до сервера. Настоящий результат НЕЛЬЗЯ подменять выдуманным:
    успешная заливка, потерянная из-за сетевого чиха, кончилась бы вторым роликом.

    Сначала пишем вердикт на диск, потом шлём с повторами; не доехало — файл остаётся,
    и следующий запуск дошлёт.
    """
    os.makedirs(VERDICTS, exist_ok=True)
    keep = os.path.join(VERDICTS, f"{rid}.json")
    with open(keep + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"release_id": rid, "claim": claim, "out": out}, f, ensure_ascii=False)
    os.replace(keep + ".tmp", keep)
    for i in range(6):
        try:
            call(f"/api/upload/{rid}/result", out, claim=claim)
            os.remove(keep)
            return True
        except Exception as exc:
            log(f"{rid}: вердикт не доехал (попытка {i + 1}/6): {exc}")
            time.sleep(20 * (i + 1))
    log(f"{rid}: вердикт сохранён в {keep} — дошлю после")
    return False


def resend_saved() -> None:
    """Дослать вердикты, не доехавшие в прошлой жизни процесса."""
    if not os.path.isdir(VERDICTS):
        return
    for name in os.listdir(VERDICTS):
        if not name.endswith(".json"):
            continue
        path = os.path.join(VERDICTS, name)
        try:
            saved = json.load(open(path, encoding="utf-8"))
            call(f"/api/upload/{saved['release_id']}/result", saved["out"],
                 claim=saved["claim"])
            os.remove(path)
            log(f"дослал сохранённый вердикт {saved['release_id']}")
        except Exception as exc:
            log(f"сохранённый вердикт {name} пока не доехал: {exc}")


def handle(task: dict) -> None:
    rid, claim = task["release_id"], task["claim_token"]
    projdir = os.path.join(WORK, rid)
    os.makedirs(projdir, exist_ok=True)
    out = None
    try:
        video = download(rid, "video", claim, os.path.join(projdir, "video.mp4"))
        if not video:
            out = {"result": {"success": False, "error": "сервер не отдал ролик"}}
            return
        cover = download(rid, "cover", claim, os.path.join(projdir, "cover.jpg"))
        size = os.path.getsize(video) // (1024 * 1024)
        say(rid, claim, stdout=f"привёз ролик {size} МБ" + (", обложка есть" if cover else ""))
        out = run_upload(task, projdir, video, cover)
    except Exception as exc:
        log(f"{rid}: сорвалось — {exc}")
        # Выдуманный провал допустим ТОЛЬКО если заливщик не успел дать свой результат.
        if out is None:
            out = {"result": {"success": False, "error": f"нода: {exc}"[:400]}}
    finally:
        delivered = deliver(rid, claim, out) if out else False
        # Файлы чистим только после доставленного вердикта: иначе при спорном итоге
        # не останется ни улик, ни ролика.
        if delivered:
            shutil.rmtree(projdir, ignore_errors=True)


def main() -> None:
    if not TOKEN:
        log("нет UPLOAD_TOKEN — останавливаюсь")
        sys.exit(1)
    os.makedirs(WORK, exist_ok=True)
    log(f"старт: сервер {SERVER}, машина {HOST}, место {WORKER}, заливщик {UPLOADER}")
    resend_saved()          # вердикты, не доехавшие в прошлой жизни
    while True:
        try:
            got = call("/api/upload/claim", {"host": HOST, "worker": WORKER})
            task = got.get("task")
            if task:
                if task.get("repeat"):
                    log(f"{task['release_id']}: сервер вернул моё же задание (ответ не доехал)")
                handle(task)
                continue
        except Exception as exc:
            log(f"опрос не удался: {exc}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
