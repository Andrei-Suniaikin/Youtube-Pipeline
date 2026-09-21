#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Готов ли стенд собрать ролик целиком — по этапам, бесплатно, до запуска.

Зачем. 20–21.09 подряд выяснилось, что конвейер стоит не там, где казалось: сертификаты
выдавались за «сеть недоступна», пакета `openai` не было вовсе, исполнитель задач был
выключен, счёт OpenAI оказался пуст, а исправный донор попал в разбор. Каждый раз это
стоило часов и выяснялось В ПРОЦЕССЕ прогона — то есть после того, как часть денег уже
потрачена.

Эта проверка отвечает на один вопрос: «можно ли запускать прямо сейчас». Все проверки
бесплатны: справочные эндпоинты, список моделей, локальные бинари. Ничего не генерируется.

Исключение — субтитры. Их выход (адрес, с которого мы ходим в YouTube) общий на всю
систему, и YouTube закрывает такие адреса за частые обращения. Тратить его на проверку
нельзя, поэтому живая проба субтитров идёт только по флагу:

    .venv/bin/python dev/preflight.py            # всё, кроме субтитров
    .venv/bin/python dev/preflight.py --subs     # плюс живая проба субтитров

Код выхода: 0 — можно запускать; 1 — есть этап, который заведомо не пройдёт.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "rewrite"))
sys.path.insert(0, os.path.join(ROOT, "cover"))

OK, WARN, BAD = "готово", "оговорка", "НЕ ПОЙДЁТ"
ROWS: list[tuple[str, str, str]] = []


def row(stage: str, state: str, note: str = "") -> None:
    ROWS.append((stage, state, note))


def _real_home() -> str:
    """Домашний каталог ЧЕЛОВЕКА, а не подменённый стендом.

    `dev/run.sh` выставляет HOME=dev/home (изоляция состояния) и именно поэтому вычисляет
    путь к ключам ДО подмены: 20.09 этот порядок был нарушен, стенд искал ключи в
    изолированном каталоге, не находил ни одного и молча работал «без ключей». Здесь
    берём настоящий домашний каталог из учётной записи, чтобы проверка не повторила ту же
    ошибку при запуске из окружения стенда.
    """
    try:
        import pwd                                                 # noqa: PLC0415
        return pwd.getpwuid(os.getuid()).pw_dir
    except Exception:                                              # noqa: BLE001
        return os.path.expanduser("~")


def keys() -> dict:
    path = os.environ.get("VS_SERVER_KEYS_PATH") or os.path.join(
        _real_home(), "video_studio_api_keys.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:                                       # noqa: BLE001
        row("файл ключей", BAD, f"{path}: {type(exc).__name__}")
        return {}


def _get(url, headers=None, timeout=15):
    import requests                                                # noqa: PLC0415
    return requests.get(url, headers=headers or {}, timeout=timeout)


def check_certs() -> None:
    import ssl                                                     # noqa: PLC0415
    if os.environ.get("SSL_CERT_FILE") or ssl.get_default_verify_paths().cafile:
        row("сертификаты", OK)
    else:
        # Без связки КАЖДЫЙ https-вызов падает и выглядит как «сеть недоступна».
        row("сертификаты", BAD, "нет связки: поднимайте стенд через dev/run.sh")


def check_fastgen(k: dict) -> None:
    key = k.get("googler") or ""
    if not key:
        row("FastGen (кадры, обложки, промпты, текст)", BAD, "ключа нет")
        return
    try:
        r = _get("https://api.fast-gen.ai/api/v6/usage", {"X-API-Key": key})
        if r.status_code != 200:
            row("FastGen", BAD, f"HTTP {r.status_code}")
            return
        d = r.json() or {}
        # Ключи квот — ПЛОСКИЕ строки с точками ("image.generations.hour"), а не
        # вложенные словари: вложенный разбор молча давал None и показывал «квоты
        # неизвестны» на исправном ключе.
        q = d.get("quotas") or {}
        img = q.get("image.generations.hour")
        tok = q.get("prompt.tokens.hour")
        exp = d.get("expiration_date")
        left = (float(exp) - time.time()) / 86400 if exp else None
        note = f"картинок/час {img}, токенов/час {tok}"
        if left is not None:
            when = dt.datetime.fromtimestamp(float(exp)).strftime("%d.%m.%Y")
            note += f"; действует до {when} ({left:.0f} дн.)"
        # Ключ держит ЧЕТЫРЕ этапа сразу — протухание останавливает конвейер целиком.
        row("FastGen", WARN if (left is not None and left < 7) else OK, note)
    except Exception as exc:                                       # noqa: BLE001
        row("FastGen", BAD, f"{type(exc).__name__}")


def check_openai(k: dict) -> None:
    key = k.get("openai") or ""
    if not key:
        row("OpenAI", WARN, "ключа нет — не нужен, пока работают обходные маршруты")
        return
    try:
        r = _get("https://api.openai.com/v1/models", {"Authorization": "Bearer " + key})
        if r.status_code == 200:
            row("OpenAI", OK, "ключ жив (баланс этим способом не виден)")
        elif r.status_code in (401, 403):
            row("OpenAI", WARN, "ключ не принят — не нужен, пока работают обходные")
        else:
            row("OpenAI", WARN, f"HTTP {r.status_code}")
    except Exception as exc:                                       # noqa: BLE001
        row("OpenAI", WARN, type(exc).__name__)


def check_alt(k: dict) -> None:
    """Прокси, которым идут промпты и анализ обложки. Пустое тело = бесплатно."""
    key = k.get("alt_llm") or ""
    base = (os.environ.get("COVER_ALT_BASE_URL") or "https://triklz27.ru/v1").rstrip("/")
    if not key:
        row("Альт-прокси (анализ обложки)", BAD, "ключа alt_llm нет")
        return
    try:
        import requests                                            # noqa: PLC0415
        r = requests.post(f"{base}/chat/completions",
                          headers={"Authorization": "Bearer " + key},
                          json={"model": os.environ.get("COVER_ALT_MODEL")
                                or "gpt-5.6-terra", "messages": []}, timeout=20)
        # 4xx про ПУСТОЙ messages = ключ и модель приняты, генерации не было.
        if r.status_code in (400, 422):
            row("Альт-прокси", OK, "ключ и модель приняты")
        elif r.status_code in (401, 403):
            row("Альт-прокси", BAD, "ключ не принят")
        else:
            row("Альт-прокси", WARN, f"HTTP {r.status_code}")
    except Exception as exc:                                       # noqa: BLE001
        row("Альт-прокси", BAD, type(exc).__name__)


def check_tts() -> None:
    try:
        import edge_tts                                            # noqa: F401,PLC0415
        row("Озвучка Edge", OK, "бесплатно, ключ не нужен")
    except Exception:                                              # noqa: BLE001
        row("Озвучка Edge", BAD, "нет пакета edge-tts")


def check_render() -> None:
    path = shutil.which("ffmpeg")
    if not path:
        row("Сборка (ffmpeg)", BAD, "не установлен: brew install ffmpeg")
        return
    try:
        v = subprocess.run([path, "-version"], capture_output=True, text=True,
                           timeout=10).stdout.splitlines()[0]
    except Exception:                                              # noqa: BLE001
        v = "версия не прочиталась"
    row("Сборка (ffmpeg)", OK, v[:60])


def check_cover_preset() -> None:
    """Пресеты обложек лежат ПО ПОЛЬЗОВАТЕЛЯМ и сеются при первом обращении.

    Поэтому их отсутствие — не приговор: «Default RU» появится сам, как только сервис
    обложек первый раз спросят. Считаем те, что уже есть, и молчим об остальном.
    """
    import glob                                                    # noqa: PLC0415
    base = os.path.join(os.environ.get("HOME") or _real_home(), "cover_presets")
    total = 0
    for f in glob.glob(os.path.join(base, "*", "presets.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                total += len(json.load(fh) or {})
        except Exception:                                          # noqa: BLE001
            pass
    row("Пресеты обложек", OK,
        f"{total} шт." if total else "пока нет — «Default RU» засеется при первом запуске")


def check_scheduler() -> None:
    off = (os.environ.get("VS_SERVER_DISABLE_BACKGROUND") or "").strip().lower()
    if off in ("1", "true", "yes"):
        row("Исполнитель задач", BAD,
            "выключен: поднимайте через dev/run-live.sh, иначе задачи просто стоят")
    else:
        row("Исполнитель задач", OK, "включён (проверяется в окружении ЭТОГО процесса)")


def check_subs() -> None:
    try:
        import subs_fetch                                          # noqa: PLC0415
        r = subs_fetch.fetch_one("https://www.youtube.com/watch?v=Oe0p_GZj3wg",
                                 lang="auto", timeout=25)
        t = getattr(r, "text", "") or ""
        row("Субтитры донора", OK if t else BAD,
            f"{len(t)} знаков" if t else (r.error or "пусто"))
    except Exception as exc:                                       # noqa: BLE001
        row("Субтитры донора", BAD, type(exc).__name__)


def main() -> int:
    live_subs = "--subs" in sys.argv
    k = keys()
    check_certs()
    check_scheduler()
    check_fastgen(k)
    check_alt(k)
    check_openai(k)
    check_tts()
    check_render()
    check_cover_preset()
    if live_subs:
        check_subs()
    else:
        row("Субтитры донора", "не проверено",
            "живая проба тратит общий выход YouTube — запустите с --subs")

    print("\nГотовность к полному прогону\n")
    width = max(len(s) for s, _, _ in ROWS)
    for stage, state, note in ROWS:
        print(f"  {stage:<{width}}  {state:<12} {note}")
    bad = [s for s, st, _ in ROWS if st == BAD]
    print()
    if bad:
        print("НЕ ПОЙДЁТ:", ", ".join(bad))
        return 1
    print("Препятствий нет: полный прогон можно запускать.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
