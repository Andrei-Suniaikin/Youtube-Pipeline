# -*- coding: utf-8 -*-
"""Распознавание речи на другой машине (MANGA_ALIGN_HOST) — обычно на Минске с видеокартой.

Зачем. Тайминги кадров ставятся по словам распознавания, и это самый долгий шаг ролика.
Замер 20.09.2026 на ролике «Реванш» (80,5 мин речи):
  сервер, faster-whisper на процессоре    — больше 30 минут, при этом VPS и так занят
                                            вызовами моделей и сборкой промптов;
  Минск, тот же faster-whisper            — 5,3 мин звука за 18 с, то есть ~5 минут на ролик.
Разница в шесть раз, а видеокарта Минска при этом простаивает: его ядра заняты вызовами
Codex, но не распознаванием.

Как. Звук едет на ту машину, там разбирается её же faster-whisper, назад едет JSON со
словами и временами — ровно того формата, что делает align.whisper_words.

Что важно:
- любая осечка (нет связи, нет библиотеки, пустой ответ) — НЕ ошибка ролика: студия
  распознаёт у себя, как раньше. MANGA_ALIGN_FALLBACK=none превращает осечку в ошибку,
  это для проверки самой настройки;
- адрес машины в журнал ролика не попадает (scrub), как у сборки и чистки;
- на той стороне файл кладётся во временную папку и удаляется после разбора.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import uuid
from typing import List, Optional

# Скрипт распознавания целиком: отправляется на ту машину stdin'ом, ничего там не хранится.
REMOTE_CODE = r'''
import json, sys
from faster_whisper import WhisperModel
path, language, size, device, ctype = sys.argv[1:6]
model = WhisperModel(size, device=device, compute_type=ctype)
segments, _info = model.transcribe(path, language=(language or None), word_timestamps=True,
                                   vad_filter=False)
out = []
for seg in segments:
    for w in (getattr(seg, "words", None) or []):
        text = (getattr(w, "word", "") or "").strip()
        if text:
            out.append({"word": text, "start": float(getattr(w, "start", 0.0) or 0.0),
                        "end": float(getattr(w, "end", 0.0) or 0.0)})
print("===СЛОВА===")
print(json.dumps(out, ensure_ascii=False))
'''
MARK = "===СЛОВА==="


def _int(env, name, default, lo, hi) -> int:
    raw = str(env.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name}: нужно целое число от {lo} до {hi}, а не {raw!r}") from None
    if not lo <= value <= hi:
        raise ValueError(f"{name}: от {lo} до {hi}")
    return value


def settings(env=None) -> dict:
    env = os.environ if env is None else env
    cfg = dict(
        host=str(env.get("MANGA_ALIGN_HOST", "")).strip(),
        label=str(env.get("MANGA_ALIGN_HOST_LABEL", "Минск")).strip() or "Минск",
        port=_int(env, "MANGA_ALIGN_PORT", 0, 0, 65535),
        key=str(env.get("MANGA_ALIGN_SSH_KEY", "")).strip(),
        python=str(env.get("MANGA_ALIGN_REMOTE_PYTHON", "python3")).strip() or "python3",
        dir=str(env.get("MANGA_ALIGN_REMOTE_DIR", "/tmp")).strip() or "/tmp",
        device=str(env.get("MANGA_ALIGN_DEVICE", "cuda")).strip() or "cuda",
        timeout=_int(env, "MANGA_ALIGN_TIMEOUT", 3600, 60, 86400),
        connect_timeout=_int(env, "MANGA_ALIGN_CONNECT_TIMEOUT", 15, 1, 600),
        fallback=str(env.get("MANGA_ALIGN_FALLBACK", "local")).strip().lower() or "local",
    )
    if cfg["device"] not in ("cuda", "cpu"):
        raise ValueError("MANGA_ALIGN_DEVICE: cuda или cpu")
    if cfg["fallback"] not in ("local", "none"):
        raise ValueError("MANGA_ALIGN_FALLBACK: local или none")
    return cfg


def enabled(cfg=None) -> bool:
    cfg = cfg or settings()
    return bool(cfg["host"])


def scrub(text: str, cfg: dict) -> str:
    """Адрес машины наружу не показываем — в журнале ролика он не нужен."""
    out = str(text or "")
    host = cfg.get("host") or ""
    if host:
        out = out.replace(host, cfg["label"])
        _user, _, addr = host.partition("@")
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


def _run(cmd: List[str], timeout: int, stdin_text: Optional[str] = None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin_text)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "", f"таймаут {timeout} с"
    except OSError as e:
        return 127, "", str(e)


def _copy(cfg: dict, local: str, remote: str) -> bool:
    cmd = ["scp", "-q", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={cfg['connect_timeout']}",
           "-o", "StrictHostKeyChecking=accept-new"]
    if cfg["port"]:
        cmd += ["-P", str(cfg["port"])]
    if cfg["key"]:
        cmd += ["-i", cfg["key"]]
    cmd += [local, f"{cfg['host']}:{remote}"]
    code, _out, _err = _run(cmd, cfg["timeout"])
    return code == 0


def words(audio_path: str, language: str, model_size: str = "base", log=print,
          cfg: Optional[dict] = None) -> Optional[List[dict]]:
    """Слова с временами, распознанные на другой машине. None — не вышло, считаем у себя."""
    cfg = cfg or settings()
    if not enabled(cfg):
        return None
    remote_file = f"{cfg['dir'].rstrip('/')}/manga_align_{uuid.uuid4().hex}.mp3"
    if not _copy(cfg, audio_path, remote_file):
        log(f"  распознавание на {cfg['label']}: звук не доехал — считаю здесь")
        return None
    inner = (f"{shlex.quote(cfg['python'])} - {shlex.quote(remote_file)} {shlex.quote(language or '')} "
             f"{shlex.quote(model_size)} {shlex.quote(cfg['device'])} "
             f"{shlex.quote('float16' if cfg['device'] == 'cuda' else 'int8')}")
    cmd = _ssh_base(cfg) + [cfg["host"], f"{inner}; rm -f {shlex.quote(remote_file)}"]
    code, out, err = _run(cmd, cfg["timeout"], stdin_text=REMOTE_CODE)
    if code != 0 or MARK not in out:
        log(f"  распознавание на {cfg['label']} не вышло ({scrub((err or out)[-200:], cfg)}) — считаю здесь")
        _run(_ssh_base(cfg) + [cfg["host"], f"rm -f {shlex.quote(remote_file)}"], 60)
        return None
    try:
        got = json.loads(out.split(MARK, 1)[1].strip())
    except ValueError:
        log(f"  распознавание на {cfg['label']}: ответ не разобрался — считаю здесь")
        return None
    if not isinstance(got, list) or not got:
        log(f"  распознавание на {cfg['label']}: слов не вернулось — считаю здесь")
        return None
    clean = [{"word": str(w.get("word") or "").strip(),
              "start": float(w.get("start") or 0.0), "end": float(w.get("end") or 0.0)}
             for w in got if isinstance(w, dict) and str(w.get("word") or "").strip()]
    if not clean:
        return None
    log(f"  распознавание на {cfg['label']}: слов {len(clean)}")
    return clean
