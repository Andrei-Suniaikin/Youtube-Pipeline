#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Автозвук: по тексту ролика подобрать фоновые слои и положить план в проект.

Работает ДО рендера (сети на рендер-ноде нет). Результат — `_RENDER_MEDIA/sfx/`:
выбранные mp3 + `sound_plan.json`. Рендер читает план сам; если файла нет —
ведёт себя как раньше.

Модель выбрана замером 02.09.2026: шесть моделей Codex размечали одинаковые
сценарии, `gpt-5.6-terra` единственная вместе с luna стабильно (3/3) отличала
спокойный ночной ролик от динамичной документалки и не нарушала правил.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess

LIB_DIR = os.environ.get("VS_SFX_LIB", "/data/sfxlib")
CODEX_HOME = os.environ.get("VS_CODEX_HOME") or "/root/.codex-server"
MODEL = os.environ.get("VS_SFX_MODEL", "gpt-5.6-terra")
TIMEOUT = int(os.environ.get("VS_SFX_TIMEOUT", "180"))
FALLBACK_BED = "room_tone"
MIN_SEGMENT = 20.0          # короче — не отдельный отрезок, а дробление ради дробления


def _library() -> dict:
    path = os.path.join(LIB_DIR, "index.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("beds") or {}
    except Exception:
        return {}


def _audio_duration(project_dir: str) -> float:
    for pattern in ("_FINAL_AUDIO_*/*.mp3", "_FINAL_AUDIO_*/*.wav", "*.mp3"):
        for path in sorted(glob.glob(os.path.join(project_dir, pattern))):
            try:
                out = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "csv=p=0", path],
                    capture_output=True, text=True, timeout=30).stdout.strip()
                value = float(out)
                if value > 30:          # озвучка ролика, а не служебный огрызок
                    return value
            except Exception:
                continue
    return 0.0


def _script_text(project_dir: str, limit: int = 6000) -> str:
    best, best_size = "", 0
    for path in glob.glob(os.path.join(project_dir, "*.txt")):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size > best_size:
            best, best_size = path, size
    if not best:
        return ""
    try:
        with open(best, "r", encoding="utf-8", errors="replace") as f:
            text = " ".join(f.read().split())
    except Exception:
        return ""
    if len(text) <= limit:
        return text
    head, mid, tail = text[:limit // 2], text[len(text) // 2:], text[-limit // 4:]
    return head + " […] " + mid[:limit // 4] + " […] " + tail


def _ask_model(text: str, duration: float, beds: dict, log=print) -> list:
    listing = "\n".join(f"- {name}: {meta.get('when', '')}" for name, meta in sorted(beds.items()))
    prompt = f"""Ты звукорежиссёр. Подбери фоновый звук под закадровый ролик длительностью {int(duration)} секунд.

Доступные фоны (только из этого списка):
{listing}

Текст ролика (сокращён):
{text}

Разбей ролик на 3-6 отрезков по смыслу: фон меняется только при смене места, времени или настроения. Отрезки идут подряд, без дыр, первый начинается с 0, последний кончается на {int(duration)}.

Верни ТОЛЬКО JSON:
{{"mode":"calm|dynamic","segments":[{{"bed":"имя_из_списка","start":0,"end":0,"why":"коротко"}}]}}

Спокойный ролик «на ночь» — берём тихие ровные фоны и меняем реже. Динамичная документалка — можно чаще. Если сомневаешься, какой фон подходит, бери room_tone."""
    env = dict(os.environ)
    env["CODEX_HOME"] = CODEX_HOME
    try:
        proc = subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", "--model", MODEL, prompt],
            capture_output=True, text=True, timeout=TIMEOUT, env=env,
            stdin=subprocess.DEVNULL, cwd="/tmp")
    except Exception as exc:
        log(f"[autosound] модель не ответила: {exc}")
        return []
    raw = proc.stdout or ""
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        log("[autosound] в ответе модели нет JSON")
        return []
    try:
        data = json.loads(match.group(0))
    except Exception:
        log("[autosound] JSON модели не разобран")
        return []
    log(f"[autosound] режим: {data.get('mode')}")
    return data.get("segments") or []


def _clean(segments: list, duration: float, beds: dict, log=print) -> list:
    out = []
    for seg in segments:
        name = str(seg.get("bed") or "").strip()
        if name not in beds:
            log(f"[autosound] фон «{name}» вне библиотеки — заменён на {FALLBACK_BED}")
            name = FALLBACK_BED
        try:
            start, end = float(seg.get("start", 0)), float(seg.get("end", 0))
        except (TypeError, ValueError):
            continue
        out.append({"bed": name, "start": max(0.0, start), "end": min(duration, end)})
    out.sort(key=lambda s: s["start"])
    fixed, prev_end = [], 0.0
    for seg in out:
        seg["start"] = prev_end                       # дыр между отрезками быть не должно
        if seg["end"] - seg["start"] < MIN_SEGMENT:
            continue
        fixed.append(seg)
        prev_end = seg["end"]
    if fixed:
        fixed[-1]["end"] = duration                   # хвост дотягиваем до конца ролика
    else:
        fixed = [{"bed": FALLBACK_BED, "start": 0.0, "end": duration}]
    return fixed


def prepare(project_dir: str, log=print) -> dict | None:
    """Собрать план автозвука для проекта. None — фон не положен, рендер идёт как раньше."""
    ready = os.path.join(project_dir, "_RENDER_MEDIA", "sfx", "sound_plan.json")
    if os.path.exists(ready):                     # план уже собран — второй раз не платим
        try:
            with open(ready, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    beds = _library()
    if not beds:
        log("[autosound] библиотека фонов недоступна")
        return None
    duration = _audio_duration(project_dir)
    if duration <= 0:
        log("[autosound] нет озвучки — плана не будет")
        return None
    text = _script_text(project_dir)
    segments = _clean(_ask_model(text, duration, beds, log) if text else [], duration, beds, log)

    target = os.path.join(project_dir, "_RENDER_MEDIA", "sfx")
    os.makedirs(target, exist_ok=True)
    plan = {"version": 1, "duration": round(duration, 2), "beds": []}
    for seg in segments:
        src = os.path.join(LIB_DIR, beds[seg["bed"]]["file"])
        if not os.path.exists(src):
            continue
        dst = os.path.join(target, os.path.basename(src))
        if not os.path.exists(dst):
            shutil.copy2(src, dst)
        plan["beds"].append({"file": os.path.basename(src),
                             "start": round(seg["start"], 2),
                             "end": round(seg["end"], 2),
                             "gain": 1.0})
    if not plan["beds"]:
        return None
    tmp = os.path.join(target, "sound_plan.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(target, "sound_plan.json"))
    log("[autosound] план готов: " + ", ".join(
        f"{b['file'][:-4]} {int(b['start'])}-{int(b['end'])}с" for b in plan["beds"]))
    return plan


if __name__ == "__main__":
    import sys
    prepare(sys.argv[1])
