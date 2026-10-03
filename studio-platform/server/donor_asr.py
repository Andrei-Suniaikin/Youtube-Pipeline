#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Текст донора без субтитров — распознаванием речи на минской машине (03.10.2026).

Владелец: «автор отключил субтитры — без нашего участия сразу на минскую машину; не
вышло — пускай падает, чтобы ребята по новой искали донора».

Почему Минск, а не VPS. YouTube не отдаёт звук ни через выходы субтитров, ни с VPS
(403 / «подтвердите, что вы не бот» — проверено 03.10 всеми способами без установки
ПО). С домашнего адреса Минска свежий yt-dlp качает звук за 10–60 с, а видеокарта там
распознаёт 3,5 часа звука моделью medium меньше чем за минуту (замер 03.10: 219 мин
звука — скачивание 11 с, распознавание 47 с). Распознавание у Минска уже есть — им
студия манхвы ставит тайминги (`align_remote.py`), тот же ssh и тот же faster-whisper.

Как. Скрипт целиком уходит на Минск через stdin (там ничего не хранится): качает звук
отдельной свежей сборкой yt-dlp (`~/.local/share/studio-asr/yt-dlp`, системный не
трогаем), распознаёт, удаляет звук и печатает текст после метки. Сюда возвращается
только текст; он ложится в тот же кэш, что и субтитры (`pub_remote._subs_cache_put`),
— и дальше выпуск идёт так, будто субтитры были.

Очередь — таблица `donor_asr` в базе кабинета рядом с кэшем субтитров: один ролик —
одна строка, сколько бы каналов его ни взяли. Работает по одной задаче за раз в фоновом
потоке студии; рестарт службы оставляет строку «running» — через STALE_SEC её берут
заново. Две попытки; потом `failed`, и выпуск встаёт с причиной словами.

Без настройки (`VS_DONOR_ASR_HOST` пуст) ничего не включается: всё как до 03.10.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time

MARK_TEXT = "===ТЕКСТ==="
MARK_ERR = "===ОШИБКА==="
MAX_ATTEMPTS = 2
STALE_SEC = 20 * 60          # «running» дольше этого без обновления — служба перезапускалась
RETRY_PAUSE_SEC = 5 * 60     # между попытками: разовый 403 YouTube проходит сам

REMOTE_CODE = r'''
import json, os, shutil, subprocess, sys, tempfile, time
url, ytdlp, size, device, ctype = sys.argv[1:6]
tmp = tempfile.mkdtemp(prefix="studio_asr_")
try:
    files, err = [], ""
    # Разовый 403 YouTube бывает и с домашнего адреса (03.10: тот же ролик через минуту
    # скачался). Вторая и третья попытки — с JS-движком node.
    for attempt, extra in enumerate(([], ["--js-runtimes", "node"], ["--js-runtimes", "node"])):
        if attempt:
            time.sleep(20)
        for f in os.listdir(tmp):
            os.remove(os.path.join(tmp, f))
        p = subprocess.run([ytdlp, "-q", "--no-warnings", "--no-playlist", *extra,
                            "-f", "bestaudio[abr<=96]/bestaudio",
                            "-o", os.path.join(tmp, "a.%(ext)s"), url],
                           capture_output=True, text=True, timeout=1800)
        files = [f for f in os.listdir(tmp) if f.startswith("a.") and not f.endswith(".part")]
        err = ((p.stderr or "") + (p.stdout or ""))[-400:]
        if p.returncode == 0 and files:
            break
    if not files:
        print("===ОШИБКА===" + json.dumps({"stage": "download", "error": err}, ensure_ascii=False))
        sys.exit(0)
    from faster_whisper import WhisperModel
    model = WhisperModel(size, device=device, compute_type=ctype)
    segments, info = model.transcribe(os.path.join(tmp, files[0]), vad_filter=True, beam_size=1)
    parts = [s.text.strip() for s in segments if s.text.strip()]
    print("===ТЕКСТ===" + json.dumps({"text": " ".join(parts), "lang": info.language,
          "seconds": info.duration}, ensure_ascii=False))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
'''


def settings(env=None) -> dict:
    env = os.environ if env is None else env

    def num(name, default):
        try:
            return int(str(env.get(name, "") or default))
        except ValueError:
            return default

    return {
        "host": str(env.get("VS_DONOR_ASR_HOST", "") or "").strip(),
        "port": num("VS_DONOR_ASR_PORT", 0),
        "key": str(env.get("VS_DONOR_ASR_SSH_KEY", "") or "").strip(),
        "python": str(env.get("VS_DONOR_ASR_PYTHON", "") or "python3").strip(),
        "ytdlp": str(env.get("VS_DONOR_ASR_YTDLP", "") or "yt-dlp").strip(),
        "model": str(env.get("VS_DONOR_ASR_MODEL", "") or "medium").strip(),
        "device": str(env.get("VS_DONOR_ASR_DEVICE", "") or "cuda").strip(),
        "timeout": num("VS_DONOR_ASR_TIMEOUT", 3600),
        "connect_timeout": num("VS_DONOR_ASR_CONNECT_TIMEOUT", 20),
        "label": str(env.get("VS_DONOR_ASR_LABEL", "") or "минской машине").strip(),
    }


def enabled(cfg=None) -> bool:
    return bool((cfg or settings())["host"])


def scrub(text: str, cfg: dict) -> str:
    """Адрес машины в журнал выпуска и на экран не попадает."""
    out = str(text or "")
    host = cfg.get("host") or ""
    if host:
        out = out.replace(host, cfg["label"])
        _user, _, addr = host.partition("@")
        if addr:
            out = out.replace(addr, cfg["label"])
    return out


# ─────────────────────────── очередь ───────────────────────────

def _db():
    import pub_remote                                              # noqa: PLC0415
    conn = pub_remote._cabinet_db()
    conn.execute("CREATE TABLE IF NOT EXISTS donor_asr("
                 "video_id TEXT PRIMARY KEY, url TEXT NOT NULL, status TEXT NOT NULL, "
                 "attempts INTEGER NOT NULL DEFAULT 0, error TEXT, chars INTEGER, lang TEXT, "
                 "seconds REAL, not_before REAL, created REAL NOT NULL, updated REAL NOT NULL)")
    return conn


def state(vid: str) -> dict:
    """Строка распознавания ролика. {} — не заказывали."""
    try:
        conn = _db()
        try:
            conn.row_factory = None
            row = conn.execute("SELECT video_id, url, status, attempts, error, chars, lang, "
                               "seconds, not_before, updated FROM donor_asr WHERE video_id=?",
                               (vid,)).fetchone()
        finally:
            conn.close()
    except Exception:                                              # noqa: BLE001
        return {}
    if not row:
        return {}
    keys = ("video_id", "url", "status", "attempts", "error", "chars", "lang", "seconds",
            "not_before", "updated")
    return dict(zip(keys, row))


def request(vid: str, url: str, now: float = None) -> dict:
    """Поставить ролик на распознавание. Уже стоит или сделан — ничего не меняет."""
    now = time.time() if now is None else now
    conn = _db()
    try:
        conn.execute("INSERT OR IGNORE INTO donor_asr(video_id,url,status,attempts,created,"
                     "updated) VALUES(?,?,'queued',0,?,?)", (vid, url, now, now))
        conn.commit()
    finally:
        conn.close()
    return state(vid)


def _next(now: float):
    """Следующая работа: стоящая в очереди или брошенная перезапуском службы."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT video_id, url, attempts FROM donor_asr WHERE "
            "(status='queued' AND COALESCE(not_before,0)<=?) OR "
            "(status='running' AND updated<?) ORDER BY created LIMIT 1",
            (now, now - STALE_SEC)).fetchone()
        if not row:
            return None
        cur = conn.execute("UPDATE donor_asr SET status='running', attempts=attempts+1, "
                           "updated=? WHERE video_id=? AND status IN ('queued','running')",
                           (now, row[0]))
        conn.commit()
        return (row[0], row[1], int(row[2] or 0) + 1) if cur.rowcount else None
    finally:
        conn.close()


def _finish(vid: str, status: str, now: float, **fields) -> None:
    conn = _db()
    try:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE donor_asr SET status=?, updated=?{', ' + sets if sets else ''} "
                     f"WHERE video_id=?", (status, now, *fields.values(), vid))
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────── Минск ───────────────────────────

def run_remote(url: str, cfg: dict) -> dict:
    """Скачать и распознать на той машине. {"text","lang","seconds"} или {"error"}."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={cfg['connect_timeout']}",
           "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30",
           "-o", "ServerAliveCountMax=4"]
    if cfg["port"]:
        cmd += ["-p", str(cfg["port"])]
    if cfg["key"]:
        cmd += ["-i", cfg["key"]]
    ctype = "float16" if cfg["device"] == "cuda" else "int8"
    inner = " ".join(shlex.quote(x) for x in (cfg["python"], "-", url, cfg["ytdlp"],
                                               cfg["model"], cfg["device"], ctype))
    cmd += [cfg["host"], inner]
    try:
        p = subprocess.run(cmd, input=REMOTE_CODE, capture_output=True, text=True,
                           timeout=cfg["timeout"])
    except subprocess.TimeoutExpired:
        return {"error": f"нет ответа за {cfg['timeout'] // 60} мин"}
    except OSError as exc:
        return {"error": f"ssh не запустился: {exc}"}
    out = p.stdout or ""
    if MARK_TEXT in out:
        try:
            return json.loads(out.split(MARK_TEXT, 1)[1].strip().splitlines()[0])
        except (ValueError, IndexError):
            return {"error": "ответ распознавания не разобрался"}
    if MARK_ERR in out:
        try:
            got = json.loads(out.split(MARK_ERR, 1)[1].strip().splitlines()[0])
        except (ValueError, IndexError):
            got = {}
        stage = {"download": "звук с YouTube не скачался"}.get(got.get("stage"), "сбой")
        return {"error": f"{stage}: {scrub(got.get('error') or '', cfg)[-200:]}".strip()}
    tail = scrub(((p.stderr or "") + out)[-200:], cfg)
    return {"error": f"машина не ответила (код {p.returncode}): {tail}".strip()}


def work_once(now: float = None, runner=None, cfg=None) -> str:
    """Сделать одну работу из очереди. Возвращает video_id сделанной или ""."""
    cfg = cfg or settings()
    runner = runner or run_remote
    now = time.time() if now is None else now
    job = _next(now)
    if not job:
        return ""
    vid, url, attempt = job
    got = runner(url, cfg) or {}
    text = str(got.get("text") or "").strip()
    later = time.time()
    if text and len(text) >= 200:
        import pub_remote                                          # noqa: PLC0415
        pub_remote._subs_cache_put(vid, text, str(got.get("lang") or ""),
                                   float(got.get("seconds") or 0))
        _finish(vid, "done", later, chars=len(text), lang=str(got.get("lang") or ""),
                seconds=float(got.get("seconds") or 0), error=None)
        return vid
    why = (str(got.get("error") or "").strip()
           or ("речи в ролике почти нет" if text or "text" in got else "пустой ответ"))
    if attempt < MAX_ATTEMPTS:
        _finish(vid, "queued", later, error=why[:300], not_before=later + RETRY_PAUSE_SEC)
    else:
        _finish(vid, "failed", later, error=why[:300])
    return vid


# ─────────────────────────── фоновый поток ───────────────────────────

_LOCK = threading.Lock()
_THREAD = None


def _loop():
    global _THREAD
    try:
        while True:
            try:
                if not work_once():
                    break
            except Exception:                                      # noqa: BLE001
                break
    finally:
        with _LOCK:
            _THREAD = None


def ensure_worker() -> bool:
    """Запустить фоновую работу, если она не идёт. Дёшево: зовётся на каждом тике."""
    global _THREAD
    if not enabled():
        return False
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return True
        _THREAD = threading.Thread(target=_loop, name="donor-asr", daemon=True)
        _THREAD.start()
        return True
