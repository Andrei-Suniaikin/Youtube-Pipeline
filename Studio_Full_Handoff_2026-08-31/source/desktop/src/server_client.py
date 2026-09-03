#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тонкий клиент VideoStudio gen-сервера (Фаза 2 переноса).

Идея: когда «серверный режим» включён, клиент НЕ запускает воркер локально, а
ставит задачу на сервер (ключи живут ТОЛЬКО на сервере) и скачивает результат.
Если сервер выключен/недоступен — вызывающий код откатывается на локальную генерацию.

Конфиг ~/video_studio_server.json:
    {"enabled": true, "base_url": "https://gen.…", "token": "usr_…"}

Только stdlib (urllib) — без новых зависимостей у клиента.
"""
import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error

CONFIG_PATH = os.path.expanduser("~/video_studio_server.json")


def load_config():
    try:
        d = json.load(open(CONFIG_PATH))
    except Exception:
        d = {}
    return {
        "enabled": bool(d.get("enabled", False)),
        "base_url": (d.get("base_url") or "").rstrip("/"),
        "token": d.get("token") or "",
    }


def is_enabled():
    """Сервер сконфигурирован и включён?"""
    c = load_config()
    return bool(c["enabled"] and c["base_url"] and c["token"])


def ping(timeout=10):
    """Сервер жив и токен валиден? → (ok, info)."""
    c = load_config()
    if not (c["base_url"] and c["token"]):
        return False, "сервер не настроен"
    try:
        st, body = _req("GET", f"{c['base_url']}/api/me", c["token"], timeout=timeout)
        return True, json.loads(body)
    except Exception as e:
        return False, str(e)[:200]


def _req(method, url, token, params=None, timeout=60):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    r = urllib.request.Request(url, method=method)
    r.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, resp.read()


def submit(job_type, prompt, *, token=None, base_url=None, timeout=30, **extra):
    c = load_config()
    base_url = (base_url or c["base_url"]).rstrip("/")
    token = token or c["token"]
    params = {"type": job_type, "prompt": prompt}
    params.update({k: v for k, v in extra.items() if v is not None})
    st, body = _req("POST", f"{base_url}/api/jobs", token, params=params, timeout=timeout)
    return json.loads(body)


def wait(job_id, *, token=None, base_url=None, poll=3, timeout=1800):
    c = load_config()
    base_url = (base_url or c["base_url"]).rstrip("/")
    token = token or c["token"]
    waited = 0
    while waited < timeout:
        st, body = _req("GET", f"{base_url}/api/jobs/{job_id}", token, timeout=30)
        d = json.loads(body)
        if d.get("status") in ("done", "error"):
            return d
        time.sleep(poll)
        waited += poll
    return {"status": "error", "error": "timeout ожидания результата с сервера"}


def download(job_id, out_path, *, token=None, base_url=None, timeout=300):
    c = load_config()
    base_url = (base_url or c["base_url"]).rstrip("/")
    token = token or c["token"]
    st, body = _req("GET", f"{base_url}/api/jobs/{job_id}/result", token, timeout=timeout)
    tmp = out_path + ".part"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, out_path)  # атомарно
    return os.path.exists(out_path) and os.path.getsize(out_path) > 0


def generate(job_type, prompt, out_path, *, poll_timeout=1800, **extra):
    """Высокоуровнево: submit → wait → download. Возвращает (ok: bool, err: str)."""
    try:
        r = submit(job_type, prompt, **extra)
    except urllib.error.HTTPError as e:
        return False, f"сервер отклонил задачу: HTTP {e.code} {e.read()[:160]!r}"
    except Exception as e:
        return False, f"сервер недоступен: {str(e)[:160]}"
    jid = r.get("job_id")
    if not jid:
        return False, f"сервер не вернул job_id: {r}"
    d = wait(jid, timeout=poll_timeout)
    if d.get("status") != "done":
        return False, d.get("error") or "статус не done"
    try:
        if download(jid, out_path):
            return True, ""
        return False, "результат не скачался / пустой"
    except Exception as e:
        return False, f"ошибка скачивания: {str(e)[:160]}"


def _post_json(path, body, timeout=60):
    c = load_config()
    data = json.dumps(body).encode("utf-8")
    r = urllib.request.Request(c["base_url"] + path, data=data, method="POST")
    r.add_header("Authorization", f"Bearer {c['token']}")
    r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read())


def b64_file(path):
    """Прочитать файл и вернуть base64 (для бинарных входов: картинки-референсы, кадры для video/transform)."""
    import base64
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def run_worker(worker, args, files, out_dir, *, files_b64=None, poll_timeout=1800):
    """Прогнать РЕАЛЬНЫЙ воркер на сервере и скачать ВСЕ выходные файлы в out_dir,
    сохраняя относительные пути (1-в-1 как писал бы локальный воркер). → (ok: bool, info: str).
    files     = {имя: текст}   — текстовые входы (paragraphs.json, source.txt).
    files_b64 = {имя: base64}  — бинарные входы (jpg-референсы, кадры). Имя можно упоминать
                                 ВНУТРИ строки-аргумента (--user_refs hero:ref1.jpg) — сервер подставит путь."""
    c = load_config()
    try:
        r = _post_json("/api/run-worker", {"worker": worker, "args": args,
                                           "files": files or {}, "files_b64": files_b64 or {}})
    except urllib.error.HTTPError as e:
        return False, f"сервер отклонил: HTTP {e.code} {e.read()[:160]!r}"
    except Exception as e:
        return False, f"сервер недоступен: {str(e)[:160]}"
    jid = r.get("job_id")
    if not jid:
        return False, f"нет job_id: {r}"
    d = wait(jid, timeout=poll_timeout)
    if d.get("status") != "done":
        return False, d.get("error") or "статус не done"
    try:
        st, body = _req("GET", f"{c['base_url']}/api/jobs/{jid}/files", c["token"], timeout=60)
        rels = json.loads(body).get("files", [])
        if not rels:
            return False, "сервер не вернул файлов"
        for rel in rels:
            dest = os.path.join(out_dir, rel)
            os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
            url = f"{c['base_url']}/api/jobs/{jid}/file/" + urllib.parse.quote(rel, safe="/")
            st2, content = _req("GET", url, c["token"], timeout=300)
            tmp = dest + ".part"
            with open(tmp, "wb") as f:
                f.write(content)
            os.replace(tmp, dest)
        return True, f"{len(rels)} файлов"
    except Exception as e:
        return False, f"ошибка скачивания: {str(e)[:160]}"


if __name__ == "__main__":
    import sys
    print("config:", load_config())
    print("ping:", ping())
