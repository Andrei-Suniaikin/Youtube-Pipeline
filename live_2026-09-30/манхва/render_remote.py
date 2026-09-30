# -*- coding: utf-8 -*-
"""Сборка клипов и склейка на другой машине (MANGA_RENDER_HOST) — обычно на Минске.

Зачем. Сборка — самый тяжёлый шаг по процессору: 20 роликов в сутки = ~17 часов работы
одного места сборки на VPS (замер 20.09: 53 мин на ролик 76 мин). На Минске 20 потоков и
он почти простаивает, там же уже чистятся кадры.

Как. Сервер считает план клипов сам (тот же код), отправляет на Минск кадры и план,
Минск делает клипы и склеивает немое видео, обратно едет один файл silent.mp4. Звук
кладётся на сервере, как раньше.

Что важно:
- сбой связи, ssh, rsync или сборки — НЕ ошибка ролика: студия молча собирает у себя
  (MANGA_RENDER_FALLBACK=none — тогда ошибка, для проверки настройки);
- папка ролика на Минске сохраняется между попытками: повтор не гонит кадры заново,
  а make_clips на той стороне узнаёт готовые клипы по подписи, как и на сервере;
- адрес машины в журнал не пишется (scrub), как в чистке кадров.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from typing import List, Optional

REMOTE_APP = "app"          # куда кладём код студии на той машине
PLAN = "clips_plan.json"


def settings(env=None) -> dict:
    env = os.environ if env is None else env
    host = str(env.get("MANGA_RENDER_HOST", "")).strip()
    cfg = dict(
        host=host,
        label=str(env.get("MANGA_RENDER_HOST_LABEL", "Минск")).strip() or "Минск",
        port=_int(env, "MANGA_RENDER_PORT", 0, 0, 65535),
        key=str(env.get("MANGA_RENDER_SSH_KEY", "")).strip(),
        dir=str(env.get("MANGA_RENDER_REMOTE_DIR", "/home/vova/manga_render")).strip() or "/home/vova/manga_render",
        python=str(env.get("MANGA_RENDER_REMOTE_PYTHON", "python3")).strip() or "python3",
        workers=_int(env, "MANGA_RENDER_REMOTE_WORKERS", 8, 1, 64),
        timeout=_int(env, "MANGA_RENDER_TIMEOUT", 7200, 60, 86400),
        connect_timeout=_int(env, "MANGA_RENDER_CONNECT_TIMEOUT", 15, 1, 600),
        fallback=str(env.get("MANGA_RENDER_FALLBACK", "local")).strip().lower() or "local",
    )
    if cfg["fallback"] not in ("local", "none"):
        raise ValueError("MANGA_RENDER_FALLBACK: укажите local или none")
    return cfg


def _int(env, name, default, lo, hi) -> int:
    raw = str(env.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name}: нужно целое число от {lo} до {hi}, а не {raw!r}") from None
    if not lo <= value <= hi:
        raise ValueError(f"{name}: нужно целое число от {lo} до {hi}, а не {value}")
    return value


def enabled(cfg=None) -> bool:
    cfg = cfg or settings()
    return bool(cfg["host"])


def scrub(text: str, cfg: dict) -> str:
    """Адрес машины наружу не показываем: в журнале ролика он не нужен."""
    out = str(text or "")
    host = cfg.get("host") or ""
    if host:
        out = out.replace(host, cfg["label"])
        user, _, addr = host.partition("@")
        if addr:
            out = out.replace(addr, cfg["label"])
    return out


def _ssh_base(cfg: dict) -> List[str]:
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={cfg['connect_timeout']}",
           "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30",
           "-o", "ServerAliveCountMax=3"]
    if cfg["port"]:
        cmd += ["-p", str(cfg["port"])]
    if cfg["key"]:
        cmd += ["-i", cfg["key"]]
    return cmd


def _rsync(cfg: dict, src: str, dst: str, extra: Optional[List[str]] = None) -> List[str]:
    ssh = " ".join(shlex.quote(x) for x in _ssh_base(cfg))
    return ["rsync", "-a", "--delete-missing-args", "-e", ssh] + (extra or []) + [src, dst]


def plan_rows(clips) -> List[dict]:
    """План клипов: путь картинки относительно папки ролика, длительность, режим движения."""
    return [{"image": c.image, "seconds": float(c.seconds), "mode": int(c.mode)} for c in clips]


def remote_dir(cfg: dict, project_id: str) -> str:
    return f"{cfg['dir']}/{project_id}"


WORKER = r"""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
import assemble as AS
root = os.path.dirname(os.path.abspath(__file__))
plan = json.load(open(os.path.join(root, "clips_plan.json"), encoding="utf-8"))
clips = [AS.Clip(image=os.path.join(root, r["image"]), seconds=r["seconds"], mode=r["mode"]) for r in plan["clips"]]
paths = AS.make_clips(clips, os.path.join(root, "clips"), workers=plan["workers"], log=print)
AS.concat_video(paths, os.path.join(root, "silent.mp4"))
print("СОБРАНО", len(paths))
"""


def build_silent(project_root: str, project_id: str, clips, out_silent: str,
                 app_dir: Optional[str] = None, cfg=None, log=print, run=None) -> bool:
    """Сделать клипы и немое видео на той машине. True — получилось, файл на месте.
    False — не вышло (и MANGA_RENDER_FALLBACK=local): собирать надо у себя."""
    cfg = cfg or settings()
    if not enabled(cfg):
        return False
    run = run or _run
    app_dir = app_dir or os.path.dirname(os.path.abspath(__file__))
    rdir = remote_dir(cfg, project_id)
    t0 = time.time()
    rows = plan_rows(clips)
    plan_path = os.path.join(project_root, PLAN)
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump({"clips": [{**r, "image": os.path.relpath(r["image"], project_root)} for r in rows],
                   "workers": cfg["workers"]}, f, ensure_ascii=False)
    try:
        code, out = run(_ssh_base(cfg) + [cfg["host"], f"mkdir -p {shlex.quote(rdir)}"], cfg["connect_timeout"] + 30)
        if code != 0:
            raise RuntimeError(f"нет связи: {out[-300:]}")
        for src, dst, extra in (
                (app_dir.rstrip("/") + "/", f"{cfg['host']}:{rdir}/{REMOTE_APP}/", ["--include=*.py", "--exclude=*"]),
                (os.path.join(project_root, "frames") + "/", f"{cfg['host']}:{rdir}/frames/", ["--include=*.png", "--include=*/", "--exclude=*"]),
                (os.path.join(project_root, "derived") + "/", f"{cfg['host']}:{rdir}/derived/", None),
                (plan_path, f"{cfg['host']}:{rdir}/{PLAN}", None)):
            if not os.path.exists(src.rstrip("/")):
                continue
            code, out = run(_rsync(cfg, src, dst, extra), cfg["timeout"])
            if code != 0:
                raise RuntimeError(f"передача не прошла: {out[-300:]}")
        worker_path = os.path.join(project_root, "render_worker.py")
        with open(worker_path, "w", encoding="utf-8") as f:
            f.write(WORKER)
        code, out = run(_rsync(cfg, worker_path, f"{cfg['host']}:{rdir}/render_worker.py"), cfg["timeout"])
        if code != 0:
            raise RuntimeError(f"передача не прошла: {out[-300:]}")
        code, out = run(_ssh_base(cfg) + [cfg["host"], f"{shlex.quote(cfg['python'])} {shlex.quote(rdir)}/render_worker.py"],
                        cfg["timeout"])
        if code != 0 or "СОБРАНО" not in out:
            raise RuntimeError(f"сборка на той стороне не прошла: {out[-400:]}")
        os.makedirs(os.path.dirname(out_silent), exist_ok=True)
        code, out = run(_rsync(cfg, f"{cfg['host']}:{rdir}/silent.mp4", out_silent), cfg["timeout"])
        if code != 0 or not (os.path.exists(out_silent) and os.path.getsize(out_silent) > 100000):
            raise RuntimeError(f"немое видео не забралось: {out[-300:]}")
        log(f"сборка на машине «{cfg['label']}»: {len(rows)} клипов за {int(time.time() - t0)} с")
        return True
    except Exception as e:  # noqa: BLE001 — любая осечка означает «собираем у себя»
        why = scrub(f"{type(e).__name__}: {e}", cfg)
        if cfg["fallback"] == "none":
            raise RuntimeError(f"сборка на машине «{cfg['label']}» не прошла: {why}") from None
        log(f"  ! сборка на машине «{cfg['label']}» не прошла ({why}) — собираю на сервере")
        return False


def _run(cmd: List[str], timeout: int):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"таймаут {timeout} с"
    except OSError as e:
        return 127, str(e)
