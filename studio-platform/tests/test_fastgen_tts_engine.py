#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Озвучка FastGen (Gemini TTS, голос algieba) подключена к студии по ТЗ от 03.10.2026.

Зачем. Lumean лежит с 26.09 («техработы»), 59 проектов студии и 166 выпусков ВВ2 стоят
на озвучке с готовым текстом и картинками. FastGen TTS входит в подписку: заказ стоит
10 кредитов часа из 600, денег сверх подписки нет, ключ тот же, что у картинок.

Проверяется ПРОВОДКА, а не провайдер (воркер — боевая копия из gen, md5 bcdbf44f, он
проверен там 03.10 сквозным прогоном):
  * движок известен студии: в списке, в провайдер-группе, в семействе ключа;
  * задача уходит в fastgen_tts_worker.py с ровно теми флагами, что у gen;
  * обратный страж: имя голоса Gemini не уезжает в чужой движок, чужой id не уезжает в FastGen;
  * папка озвучки есть у модуля рендера — иначе `select_narration` ищет
    `__unknown_audio_engine__`, и рендер уходит в error;
  * лимиты: своя дорожка, общий счёт ключа картинок озвучку НЕ считает.

main.py на импорте трогает файлы, поэтому спрашиваем его подпроцессом со своим HOME.

    .venv/bin/python tests/test_fastgen_tts_engine.py
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


_ПРОБА = r'''
import json, os, sys
sys.path.insert(0, os.path.join(%(base)r, "server"))
sys.path.insert(0, os.path.join(%(base)r, "workers"))
sys.path.insert(0, os.path.join(%(base)r, "render"))
import main as M
tmp = os.environ["VS_PROBE_TMP"]
proj = os.path.join(tmp, "p_test")
os.makedirs(proj, exist_ok=True)
open(os.path.join(proj, "source.txt"), "w", encoding="utf-8").write("Проба озвучки. " * 20)

def cmd(params):
    worker, argv = M._build_cmd("tts", proj, dict(params))
    return {"worker": worker, "argv": argv}

R = {}
R["в списке движков"] = "fastgen" in M.TTS_ENGINES
R["провайдер-группа"] = M._TTS_ENG_PROV.get("fastgen")
R["семейство ключа"] = M._provider_credential_family("fastgen_tts")
R["папка рендера"] = M._ENGINE_AUDIO_DIR.get("fastgen")
R["вендор общего счёта"] = M._key_budget_vendor("fastgen_tts")
R["лимиты заданы"] = "fastgen_tts" in M.PROVIDER_LIMITS
R["потолок задач"] = M._PROVIDER_HARD_JOB_CAPS.get("fastgen_tts")
R["предел аккаунта"] = M._PROVIDER_ACCOUNT_LIMITS.get("fastgen_tts", (None,))[0]
R["строка админки"] = any(r[0] == "fastgen_tts" for r in M._SERVER_LIMIT_ROWS)
R["бюджет вызовов"] = (M._PROVIDER_CALL_BUDGET_DEFAULT.get("fastgen_tts"),
                       M._PROVIDER_CALL_BUDGET_HARD.get("fastgen_tts"))
R["НЕ во взвешенном бюджете картинок"] = "fastgen_tts" not in M._PROVIDER_BUDGET_HARD
R["алгиеба"] = cmd({"engine": "fastgen", "voice": "algieba"})
R["чужой id в fastgen"] = cmd({"engine": "fastgen", "voice": "AB9XsbSA4eLG12t2myjN"})
R["пустой голос"] = cmd({"engine": "fastgen", "voice": ""})
R["стиль сцены"] = cmd({"engine": "fastgen", "voice": "charon", "tts_scene": "  спокойный   рассказчик "})
R["без кэша"] = cmd({"engine": "fastgen", "voice": "algieba", "use_cache": "0"})
R["algieba уехала в voicer"] = cmd({"engine": "voicer", "voice": "algieba"})
R["отчёт админки"] = M._provider_alloc_report("fastgen_tts")["per_job"]
print(json.dumps(R, ensure_ascii=False))
'''


def проба() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        env["HOME"] = os.path.join(BASE, "dev", "home")
        env["VS_SERVER_DISABLE_BACKGROUND"] = "1"
        env["VS_SERVER_DB"] = os.path.join(tmp, "t.db")
        env["VS_TENANTS_DIR"] = os.path.join(tmp, "tenants")
        env["VS_SERVER_KEYS_PATH"] = os.path.join(tmp, "keys.json")
        env["VS_PROBE_TMP"] = tmp
        for имя in ("FASTGEN_TTS_MODEL", "FASTGEN_TTS_MAX_CHARS", "FASTGEN_TTS_PARALLEL",
                    "FASTGEN_TTS_REORDER_UNCONFIRMED_MINUTES"):
            env.pop(имя, None)
        out = subprocess.run([sys.executable, "-c", _ПРОБА % {"base": BASE}],
                             capture_output=True, text=True, env=env, timeout=240)
    if out.returncode != 0:
        raise SystemExit("подпроцесс упал:\n" + out.stderr[-4000:])
    return json.loads((out.stdout.strip().splitlines() or ["{}"])[-1])


def аргумент(argv, флаг):
    return argv[argv.index(флаг) + 1] if флаг in argv else None


def main() -> int:
    R = проба()

    print("Движок известен студии во всех трёх местах")
    check("в списке движков", R["в списке движков"], True)
    check("провайдер-группа очереди — fastgen_tts", R["провайдер-группа"], "fastgen_tts")
    check("ключ — семейство googler, тот же, что у картинок", R["семейство ключа"], "googler")

    print("\nПапка озвучки известна модулю рендера")
    # Без этой пары select_narration ищет __unknown_audio_engine__ и рендер уходит в error.
    check("fastgen → _FINAL_AUDIO_FASTGEN", R["папка рендера"], "_FINAL_AUDIO_FASTGEN")

    print("\nЗадача уходит в воркер FastGen с флагами как у gen")
    a = R["алгиеба"]
    check("воркер — fastgen_tts_worker.py", a["worker"], "fastgen_tts_worker.py")
    check("голос", аргумент(a["argv"], "--voice"), "algieba")
    check("модель", аргумент(a["argv"], "--model"), "aistudio/gemini-3.8-flash-tts")
    check("блок 6000 знаков — то же число, что в gen", аргумент(a["argv"], "--max_chars"), "6000")
    check("перезаказ без ответа через 15 мин",
          аргумент(a["argv"], "--reorder_unconfirmed_minutes"), "15")
    check("папка озвучки передана воркеру", аргумент(a["argv"], "--audio_dir_name"),
          "_FINAL_AUDIO_FASTGEN")
    check("режим «весь текст»", "--full_text" in a["argv"], True)
    check("блоков разом — число, не больше потолка 8",
          1 <= int(аргумент(a["argv"], "--parallel")) <= 8, True)
    check("кэш по умолчанию включён", "--use_cache" in a["argv"], True)
    check("без кэша — флага нет", "--use_cache" in R["без кэша"]["argv"], False)
    check("стиль сцены ужат до одной строки",
          аргумент(R["стиль сцены"]["argv"], "--scene"), "спокойный рассказчик")

    print("\nГолос: чужой id в FastGen не уезжает, пустой — algieba")
    check("UUID/VGC-id заменён на algieba", аргумент(R["чужой id в fastgen"]["argv"], "--voice"), "algieba")
    check("пустой голос → algieba", аргумент(R["пустой голос"]["argv"], "--voice"), "algieba")

    print("\nОбратный страж: имя Gemini не уезжает в чужой движок")
    # Без него voicer/VGC получили бы voice_id=algieba и упали, а Lumean — --template_id algieba.
    v = R["algieba уехала в voicer"]
    check("voicer получил свой умолчательный голос, а не algieba",
          аргумент(v["argv"], "--voice_id") != "algieba", True)

    print("\nЛимиты: своя дорожка, картинки не задеты")
    check("лимиты заданы", R["лимиты заданы"], True)
    check("потолок задач на ключ — как в gen", R["потолок задач"], 8)
    check("предел аккаунта — 20 потоков речи", R["предел аккаунта"], 20)
    check("строка в /server-limits", R["строка админки"], True)
    check("бюджет вызовов 8 из 20", R["бюджет вызовов"], [8, 20])
    check("во взвешенный бюджет картинок НЕ добавлен", R["НЕ во взвешенном бюджете картинок"], True)
    check("общий счёт физического ключа озвучку не считает", R["вендор общего счёта"], "")
    check("админка показывает потоки на проект, а не 1", R["отчёт админки"] >= 2, True)

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
