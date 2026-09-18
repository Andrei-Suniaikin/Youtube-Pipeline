#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Настройки Lumean задаются у нас и уезжают на каждый заказ (18.09.2026).

Владелец просил перенести в приложение все настройки шаблона, кроме «настройки вывода».
Создавать шаблоны у провайдера для этого не нужно: заказ принимает `config_override` той
же формы, что и сам шаблон (сверено по живому `GET /api/public/templates`).

Без сети: проверяются сборка `config_override` в кабинете, разбор аргумента воркером и
то, что смена настроек делает озвучку ДРУГОЙ (оплаченный файл не переиспользуется).

    .venv/bin/python tests/test_lumean_settings.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(BASE, "workers"))

import tts_worker as W                                            # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def cabinet_override(params: dict):
    """`_lumean_config_override` кабинета — отдельным процессом: main.py на импорте
    сеет тестового пользователя и печатает лишнее (приём из tests/test_gpt_image_25.py)."""
    env = dict(os.environ)
    env["HOME"] = os.path.join(BASE, "dev", "home")
    env["VS_SERVER_DISABLE_BACKGROUND"] = "1"
    with tempfile.TemporaryDirectory() as tmp:
        env["VS_SERVER_DB"] = os.path.join(tmp, "t.db")
        env["VS_TENANTS_DIR"] = os.path.join(tmp, "tenants")
        code = ("import sys, json; sys.path[:0] = [%r, %r]\n"
                "import main\n"
                "try:\n"
                "    print(json.dumps({'ok': main._lumean_config_override(%r)}, ensure_ascii=False))\n"
                "except RuntimeError as exc:\n"
                "    print(json.dumps({'err': str(exc)}, ensure_ascii=False))\n"
                % (os.path.join(BASE, "server"), os.path.join(BASE, "workers"), params))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env=env, timeout=180)
    if out.returncode != 0:
        return {"err": f"импорт не удался: {out.stderr.strip()[-200:]}"}
    return json.loads((out.stdout.strip().splitlines() or ["{}"])[-1])


def main() -> int:
    print("Кабинет собирает config_override из настроек проекта")
    check("пустые настройки — ничего не шлём (всё как в шаблоне)",
          cabinet_override({}), {"ok": {}})
    full = cabinet_override({"lum_stability": "0.4", "lum_similarity": "0.8",
                             "lum_style": "0.2", "lum_speed": "1.1",
                             "lum_speaker_boost": "1", "lum_normalization": "on",
                             "lum_chunk": "1500", "lum_pauses": "1", "lum_stress": "0"})
    check("настройки ложатся теми же именами, что у шаблона Lumean", full,
          {"ok": {"tts_settings": {"apply_text_normalization": "on",
                                   "voice_settings": {"stability": 0.4,
                                                      "similarity_boost": 0.8,
                                                      "style": 0.2, "speed": 1.1,
                                                      "use_speaker_boost": True}},
                  "worker_settings": {"chunk_size": 1500},
                  "pause_settings": {"enabled": True},
                  "stress_settings": {"enabled": False}}})
    check("«настройки вывода» мы не переносим — их задаёт кабинет (mp3)",
          "output_settings" in json.dumps(full), False)
    for bad, why in (({"lum_stability": "5"}, "0.0 до 1.0"),
                     ({"lum_speed": "три"}, "нужно число"),
                     ({"lum_normalization": "да"}, "auto, on или off"),
                     ({"lum_chunk": "999999"}, "от 100 до 20000")):
        got = cabinet_override(bad)
        check(f"отказ на {list(bad)[0]}: {why}", why in str(got.get("err", "")), True)

    print("\nВоркер кладёт наши настройки в заказ, не трогая формат вывода")
    saved = dict(W.TTS_CONFIG_OVERRIDE)
    saved_post = W.requests.request
    seen = {}

    class R:
        status_code = 500
        text = "stub"

    def fake(method, url, json=None, headers=None, timeout=None, **kw):
        seen["body"] = json
        return R()

    saved_key = os.environ.get("LUMEAN_API_KEY")
    try:
        # Ключ нужен, чтобы дойти до сборки тела запроса; сети здесь нет — стоит
        # подставной ответ, и заказ не создаётся.
        os.environ["LUMEAN_API_KEY"] = "проверочный-ключ"
        # Воркер ходит через requests.request (см. `_request_json`), а не requests.post.
        W.requests.request = fake
        W.TTS_CONFIG_OVERRIDE.clear()
        W.TTS_CONFIG_OVERRIDE.update({
            "tts_settings": {"voice_settings": {"stability": 0.4}},
            "worker_settings": {"chunk_size": 1500}})
        try:
            W.create_task("текст", voice_uuid="tpl-1", model_uuid="eleven_v3")
        except Exception:
            pass          # заказ намеренно не создаётся: подставной ответ — 500
        body = seen.get("body") or {}
        over = body.get("config_override") or {}
        check("наши настройки ушли в заказ",
              over.get("tts_settings", {}).get("voice_settings"), {"stability": 0.4})
        check("модель тоже (и не затёрла voice_settings)",
              over.get("tts_settings", {}).get("model_id"), "eleven_v3")
        check("раздел воркера ушёл", over.get("worker_settings"), {"chunk_size": 1500})
        check("формат вывода остался нашим", over.get("output_settings"),
              {"audio_format": "mp3"})
        check("шаблон передан как был", body.get("template_id"), "tpl-1")

        print("\nСмена настроек делает озвучку другой — оплаченный файл не подставится")
        W.TTS_CONFIG_OVERRIDE.clear()
        one = W._receipt_identity("текст", "tpl-1", "eleven_v3",
                                  {"config": '{"tts_settings": {"voice_settings": {"stability": 0.4}}}'})
        two = W._receipt_identity("текст", "tpl-1", "eleven_v3",
                                  {"config": '{"tts_settings": {"voice_settings": {"stability": 0.9}}}'})
        same = W._receipt_identity("текст", "tpl-1", "eleven_v3",
                                   {"config": '{"tts_settings": {"voice_settings": {"stability": 0.4}}}'})
        check("другая стабильность — другой отпечаток", one == two, False)
        check("те же настройки — тот же отпечаток", one == same, True)

        print("\nЖурнал платных вызовов считает так же, иначе он отдаст старый файл")
        # Квитанция знала про настройки с самого начала, а журнал — нет: он решал
        # «этот вызов уже оплачен и доставлен» по шаблону, модели, скорости и тексту.
        # То есть сменил стабильность — и journal.reserve отвечал skip, подставляя
        # озвучку со СТАРЫМИ настройками. Отпечаток журнала обязан меняться вместе с
        # настройками — и оставаться прежним, когда настроек нет вовсе.
        from call_journal import CallJournal
        def fp():
            return CallJournal.fingerprint(
                template_id="tpl-1", model_id="eleven_v3", speed="1.0",
                text_sha="abc",
                config=(json.dumps(W.TTS_CONFIG_OVERRIDE, ensure_ascii=False, sort_keys=True)
                        if W.TTS_CONFIG_OVERRIDE else ""))
        W.TTS_CONFIG_OVERRIDE.clear()
        bare = fp()
        W.TTS_CONFIG_OVERRIDE.update({"tts_settings": {"voice_settings": {"stability": 0.4}}})
        soft = fp()
        W.TTS_CONFIG_OVERRIDE.clear()
        W.TTS_CONFIG_OVERRIDE.update({"tts_settings": {"voice_settings": {"stability": 0.9}}})
        hard = fp()
        W.TTS_CONFIG_OVERRIDE.clear()
        check("без настроек отпечаток прежний — старые оплаченные вызовы не пересчитаются",
              bare, CallJournal.fingerprint(template_id="tpl-1", model_id="eleven_v3",
                                            speed="1.0", text_sha="abc"))
        check("настройки появились — отпечаток другой", bare == soft, False)
        check("другая стабильность — снова другой", soft == hard, False)
    finally:
        W.requests.request = saved_post
        if saved_key is None:
            os.environ.pop("LUMEAN_API_KEY", None)
        else:
            os.environ["LUMEAN_API_KEY"] = saved_key
        W.TTS_CONFIG_OVERRIDE.clear()
        W.TTS_CONFIG_OVERRIDE.update(saved)

    print("\nВоркер не пропускает разделы, которых не понимает")
    out = subprocess.run(
        [sys.executable, os.path.join(BASE, "workers", "tts_worker.py"),
         "--source_file", os.devnull, "--project_dir", tempfile.gettempdir(),
         "--tts_config", '{"чужой_раздел": {"x": 1}}'],
        capture_output=True, text=True, timeout=120)
    check("неизвестный раздел настроек — отказ до заказа",
          "Неизвестные разделы настроек" in (out.stdout + out.stderr), True)
    out = subprocess.run(
        [sys.executable, os.path.join(BASE, "workers", "tts_worker.py"),
         "--source_file", os.devnull, "--project_dir", tempfile.gettempdir(),
         "--tts_config", "не json"],
        capture_output=True, text=True, timeout=120)
    check("не-JSON в настройках — отказ до заказа",
          "не JSON" in (out.stdout + out.stderr), True)

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
