"""Общий семафор лимита подписки Claude для всех сервисов этой машины.

Подписка одна, а сервисов, которые в неё ходят, несколько: rewrite-server (тексты)
и vsgen (планы сцен, главы). До этого модуля каждый вёл СВОЙ кулдаун в своей памяти,
и пока один честно ждал сброса окна, второй продолжал долбить тот же исчерпанный
токен — лишние вызовы, лишний шум в логах и лишние отказы.

Здесь общий файл-состояние: кто заметил лимит, тот записал время сброса, а видят все.

Формат `/var/lib/claude_limits/state.json`:
    {"<sha256(token)[:16]>": {"until": epoch, "kind": "limit|auth|strikes",
                              "who": "rewrite", "why": "...", "mask": "sk-ant-oat0…AAAA",
                              "at": epoch}}

Правила, ради которых он такой:
  • ключ — ХЕШ токена, сам токен в файл не попадает никогда; для человека — маска;
  • запись только удлиняет сон: повторный отказ не будит ключ раньше;
  • чтение никогда не бросает исключение и не блокирует — на горячем пути ошибка
    семафора не должна ронять работу, максимум «не знаю, считаем живым»;
  • писатели сериализуются flock, файл заменяется атомарно (tmp + os.replace);
  • просроченные записи подчищаются при следующей записи, чтение их просто игнорирует.

  python3 -m claude_limits          показать состояние
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from typing import Dict, Tuple

__all__ = ["mark_limited", "limited_until", "limited_state", "clear", "snapshot", "STATE_PATH"]

STATE_DIR = os.environ.get("CLAUDE_LIMITS_DIR", "/var/lib/claude_limits")
STATE_PATH = os.path.join(STATE_DIR, "state.json")

_CACHE: Dict[str, dict] = {}
_CACHE_STAMP: Tuple[float, float] = (0.0, 0.0)      # (mtime, когда читали)
_CACHE_TTL = 2.0                                     # сек: горячий путь зовут часто


def _key(token) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8", "replace")).hexdigest()[:16]


def _mask(token) -> str:
    v = str(token or "")
    if not v:
        return "—"
    return (v[:12] + "…" + v[-4:]) if len(v) > 20 else ("…" + v[-4:])


def _read_raw() -> Dict[str, dict]:
    """Прочитать файл целиком. Любая беда → пустое состояние: «не знаю» безопаснее отказа."""
    global _CACHE, _CACHE_STAMP
    try:
        mtime = os.path.getmtime(STATE_PATH)
    except OSError:
        return {}
    now = time.time()
    if mtime == _CACHE_STAMP[0] and (now - _CACHE_STAMP[1]) < _CACHE_TTL:
        return _CACHE
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except Exception:                       # noqa: BLE001 — битый файл не повод падать
        data = {}
    _CACHE = data
    _CACHE_STAMP = (mtime, now)
    return data


def limited_state(token) -> dict:
    """Запись про этот токен, если он сейчас спит, иначе {}.

    Ключи: until (epoch), kind, who, why, mask.
    """
    rec = _read_raw().get(_key(token))
    if not isinstance(rec, dict):
        return {}
    try:
        until = float(rec.get("until") or 0)
    except (TypeError, ValueError):
        return {}
    return dict(rec) if until > time.time() else {}


def limited_until(token) -> float:
    """Время пробуждения токена или 0, если он свободен."""
    rec = limited_state(token)
    return float(rec.get("until") or 0) if rec else 0.0


def _write(mutate) -> None:
    """Под flock: прочитать свежее, применить mutate(dict), атомарно записать."""
    global _CACHE_STAMP
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        lock_path = STATE_PATH + ".lock"
        import fcntl
        with open(lock_path, "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    with open(STATE_PATH, encoding="utf-8") as fh:
                        data = json.load(fh)
                    if not isinstance(data, dict):
                        data = {}
                except Exception:           # noqa: BLE001
                    data = {}
                now = time.time()
                # чистим просроченное здесь: чтение не должно писать на диск
                for k in [k for k, v in data.items()
                          if not isinstance(v, dict) or float(v.get("until") or 0) <= now]:
                    data.pop(k, None)
                mutate(data)
                fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".state-")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=1)
                os.chmod(tmp, 0o600)
                os.replace(tmp, STATE_PATH)
                _CACHE_STAMP = (0.0, 0.0)   # свои же изменения перечитать сразу
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except Exception:                       # noqa: BLE001
        pass                                # семафор — помощник, а не точка отказа


def mark_limited(token, until_ts, kind="limit", who="", why="") -> float:
    """Отметить, что токен упёрся и спит до until_ts. Сон только удлиняется."""
    if not token:
        return 0.0
    try:
        until = float(until_ts or 0)
    except (TypeError, ValueError):
        return 0.0
    if until <= time.time():
        return 0.0
    k, box = _key(token), {"until": until}

    def mutate(data):
        cur = data.get(k) or {}
        box["until"] = max(until, float(cur.get("until") or 0))
        data[k] = {"until": box["until"], "kind": str(kind or "limit"),
                   "who": str(who or ""), "why": " ".join(str(why or "").split())[:180],
                   "mask": _mask(token), "at": time.time()}

    _write(mutate)
    return box["until"]


def clear(token, who="") -> None:
    """Токен ответил успешно — будим его для всех сервисов сразу."""
    if not token:
        return
    k = _key(token)
    _write(lambda data: data.pop(k, None))


def snapshot() -> Dict[str, dict]:
    """Состояние для человека: маски и сроки, без секретов."""
    now = time.time()
    out = {}
    for k, v in _read_raw().items():
        if isinstance(v, dict) and float(v.get("until") or 0) > now:
            out[k] = {"mask": v.get("mask", "—"), "kind": v.get("kind", "?"),
                      "who": v.get("who", "?"), "why": v.get("why", ""),
                      "осталось_сек": int(float(v["until"]) - now),
                      "проснётся": time.strftime("%d.%m %H:%M", time.localtime(float(v["until"])))}
    return out


if __name__ == "__main__":
    состояние = snapshot()
    if not состояние:
        print(f"свободно: спящих токенов нет ({STATE_PATH})")
    else:
        print(f"{'маска':22} {'причина':9} {'заметил':9} {'проснётся':14} почему")
        for rec in состояние.values():
            print(f"{rec['mask']:22} {rec['kind']:9} {rec['who']:9} "
                  f"{rec['проснётся']:14} {rec['why'][:60]}")
