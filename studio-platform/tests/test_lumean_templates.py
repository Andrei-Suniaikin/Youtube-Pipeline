#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Шаблоны озвучки Lumean собираются из нашего интерфейса (18.09.2026).

Проверено живыми запросами к провайдеру: `POST /api/public/templates` (обязательны
service_key, name, config) отвечает 201 и возвращает шаблон вместе с `id`; у шаблона
есть PATCH и DELETE. Здесь — наша половина: сборка тела запроса, разбор ответа в поля
формы и отказы ДО обращения к провайдеру. Без сети и без ключа.

    .venv/bin/python tests/test_lumean_templates.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def call(fn: str, arg) -> dict:
    """Функция пульта отдельным процессом: web/app.py на импорте поднимает кабинет."""
    env = dict(os.environ)
    env["HOME"] = os.path.join(BASE, "dev", "home")
    env["VS_SERVER_DISABLE_BACKGROUND"] = "1"
    with tempfile.TemporaryDirectory() as tmp:
        env["VS_SERVER_DB"] = os.path.join(tmp, "t.db")
        env["VS_DEV_DB"] = os.path.join(tmp, "t.db")
        env["VS_TENANTS_DIR"] = os.path.join(tmp, "tenants")
        code = (
            "import sys, json; sys.path[:0] = [%r, %r, %r]\n"
            "from fastapi import HTTPException\n"
            "import app\n"
            "try:\n"
            "    print(json.dumps({'ok': getattr(app, %r)(%r)}, ensure_ascii=False, default=str))\n"
            "except HTTPException as exc:\n"
            "    print(json.dumps({'code': exc.status_code, 'err': exc.detail}, ensure_ascii=False))\n"
            % (os.path.join(BASE, "web"), os.path.join(BASE, "server"),
               os.path.join(BASE, "workers"), fn, arg))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env=env, timeout=240)
    if out.returncode != 0:
        return {"err": f"импорт не удался: {out.stderr.strip()[-300:]}"}
    return json.loads((out.stdout.strip().splitlines() or ["{}"])[-1])


def main() -> int:
    print("Тело запроса к Lumean собирается из полей формы")
    made = call("_lumean_template_payload", {
        "name": "RU Андрей", "service_key": "elevenlabs", "voice_id": "voice-1",
        "model_id": "eleven_v3", "stability": "0.4", "similarity_boost": "0.8",
        "style": "0.2", "speed": "1.1", "use_speaker_boost": "1",
        "apply_text_normalization": "on", "chunk_size": "1500",
        "pauses": "1", "stress": "0"})
    check("обязательные поля провайдера на месте",
          sorted((made.get("ok") or {}).keys()), ["config", "name", "service_key"])
    check("настройки голоса ложатся в config той же формой, что у шаблона",
          (made.get("ok") or {}).get("config"),
          {"tts_settings": {"voice_id": "voice-1", "model_id": "eleven_v3",
                            "apply_text_normalization": "on",
                            "voice_settings": {"stability": 0.4, "similarity_boost": 0.8,
                                               "style": 0.2, "speed": 1.1,
                                               "use_speaker_boost": True}},
           "output_settings": {"audio_format": "mp3"},
           "worker_settings": {"chunk_size": 1500},
           "pause_settings": {"enabled": True},
           "stress_settings": {"enabled": False}})
    check("формат вывода задаём мы: кабинет собирает ролик из mp3",
          (made.get("ok") or {})["config"]["output_settings"], {"audio_format": "mp3"})

    print("\nОтказы — до обращения к провайдеру, его словами не пользуемся")
    for values, why in (
            ({"voice_id": "v"}, "название"),
            ({"name": "Имя"}, "ID голоса"),
            ({"name": "Имя", "voice_id": "v", "service_key": "чужой"}, "сервис"),
            ({"name": "Имя", "voice_id": "v", "stability": "5"}, "stability"),
            ({"name": "Имя", "voice_id": "v", "speed": "три"}, "speed"),
            ({"name": "Имя", "voice_id": "v", "apply_text_normalization": "да"},
             "нормализация"),
            ({"name": "Имя", "voice_id": "v", "chunk_size": "999999"}, "размер куска")):
        got = call("_lumean_template_payload", values)
        check(f"отказ: {why}", (got.get("code"), why in str(got.get("err", ""))), (400, True))

    print("\nОтвет провайдера разбирается обратно в поля формы")
    back = call("_lumean_template_values", {
        "name": "RU Андрей", "service_key": "heygen",
        "config": {"tts_settings": {"voice_id": "v-9", "model_id": "eleven_v3",
                                    "apply_text_normalization": "auto",
                                    "voice_settings": {"stability": 0.5, "speed": 1,
                                                       "use_speaker_boost": True}},
                   "worker_settings": {"chunk_size": 2000},
                   "pause_settings": {"enabled": False}}})
    vals = back.get("ok") or {}
    check("имя, сервис и голос", (vals.get("name"), vals.get("service_key"), vals.get("voice_id")),
          ("RU Андрей", "heygen", "v-9"))
    check("числа и переключатели",
          (vals.get("stability"), vals.get("speed"), vals.get("use_speaker_boost"),
           vals.get("chunk_size"), vals.get("pauses")),
          (0.5, 1, "1", 2000, "0"))
    check("незаданное в шаблоне поле приходит пустым, а не выдуманным",
          vals.get("similarity_boost"), "")

    print("\nБез ключа Lumean экран открывается и честно говорит причину")
    empty = call("_lumean_field_options", None)
    check("варианты полей собираются и без провайдера",
          sorted((empty.get("ok") or {}).keys()),
          ["apply_text_normalization", "model_id", "service_key"])

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
