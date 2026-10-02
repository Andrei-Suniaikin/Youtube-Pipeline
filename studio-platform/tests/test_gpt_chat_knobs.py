#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Адрес чата движка и имя модели переставляются окружением, и доходят до воркера (02.10.2026).

Повод. С 16:28 02.10 прогоны падали на первом же шаге: «У канала промптов "openai"
исчерпан счёт/лимит: You have no credits remaining», а за ними обрывался этап картинок
(«конвейер оборван»). При этом канал промптов был настроен ВЕРНО — политика в файле
ключей с 02.09 разрешает только `codex`, а openai запрещён вовсе.

Падал не канал промптов, а предварительные анализы — `visual_context` и
`semantic_beats`. Они политику не спрашивают никогда: адрес `OPENAI_API_URL` был прибит
в коде к `api.openai.com`, и они постили туда своим ключом OpenAI. Счёт пуст — прогон
мёртв, а из интерфейса это читалось как поломка у нас.

Две половины починки, и каждая без другой бесполезна:
  1. воркер читает адрес и модели из окружения (умолчания прежние — вернуться на OpenAI
     можно одной переменной, а не откатом кода);
  2. сервер эти ручки ПЕРЕДАЁТ. Окружение воркера строится по белому списку
     (`_sanitized_worker_env`), поэтому до 02.10 ручка в воркере существовала, но в бою
     была мертва — ровно как `VS_GOOGLER_BASE`, который работает только при запуске
     воркера из оболочки.

Отдельно проверяется, что адрес и модель ходят парой: у FastGen модели `gpt-4.1-nano`
не существует (живой вызов отвечает 400 «No supported model found»), и переставить
адрес без модели значит сломать прогон другим способом.

    .venv/bin/python tests/test_gpt_chat_knobs.py
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.abspath(os.path.join(HERE, ".."))
WORKER = os.path.join(BASE, "workers", "googler_worker.py")

FAILS = []
РУЧКИ = ("VS_GPT_CHAT_URL", "VS_GPT_MODEL", "VS_SCENE_GPT_MODEL", "VS_GPT_CHAT_KEY")


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def из_воркера(имена, окружение) -> dict:
    """Значения констант воркера при заданном окружении.

    Воркер целиком не импортируем: 13 тысяч строк тянут провайдеров и ключи. Берём
    только строки присваивания — их ровно столько, сколько проверяем.
    """
    src = io.open(WORKER, encoding="utf-8").read()
    куски = []
    for имя in имена:
        m = re.search(r"^%s = (.+?)(?=\n[A-Z_]|\n#|\n\n)" % re.escape(имя), src, re.M | re.S)
        if not m:
            return {}
        куски.append(f"{имя} = {m.group(1).strip()}")
    снимок = "import os, json\n" + "\n".join(куски) + "\nprint(json.dumps({%s}))" % (
        ", ".join(f'"{и}": {и}' for и in имена))
    окр = dict(os.environ)
    for имя in РУЧКИ:
        окр.pop(имя, None)
    окр.update(окружение)
    out = subprocess.run([sys.executable, "-c", снимок], capture_output=True,
                         text=True, env=окр, timeout=60)
    if out.returncode != 0:
        raise SystemExit("снимок воркера не собрался:\n" + out.stderr[-2000:])
    return json.loads(out.stdout.strip().splitlines()[-1])


_ПРОБА_ЗАГОЛОВКА = r'''
import json, os, sys
os.environ["OPENAI_API_KEY"] = "sk-ключ-openai"
sys.path.insert(0, os.path.join(%(base)r, "workers"))
import googler_worker as W
try:
    print(json.dumps(W._openai_chat_headers(), ensure_ascii=False))
except Exception as e:
    print(json.dumps({"ошибка": str(e)}, ensure_ascii=False))
'''


def заголовки(окружение) -> dict:
    """Что воркер положит в Authorization при заданном окружении."""
    окр = dict(os.environ)
    окр["VS_TEST_NO_NET"] = "1"
    for имя in РУЧКИ:
        окр.pop(имя, None)
    окр.update(окружение)
    out = subprocess.run([sys.executable, "-c", _ПРОБА_ЗАГОЛОВКА % {"base": BASE}],
                         capture_output=True, text=True, env=окр, timeout=180)
    if out.returncode != 0:
        raise SystemExit("воркер не импортировался:\n" + out.stderr[-3000:])
    return json.loads(out.stdout.strip().splitlines()[-1])


_ПРОБА_СЕРВЕРА = r'''
import json, os, sys
sys.path.insert(0, os.path.join(%(base)r, "server"))
import main
main.keys = lambda: {"googler": "fg-ключ-из-файла", "openai": "sk-ключ-openai"}
env = main._key_env({"id": 1, "name": "dev", "role": "admin", "keys": None}, shared=False)
print(json.dumps({k: env.get(k) for k in %(knobs)r}))
'''


def через_сервер(окружение) -> dict:
    """Что сервер реально положит в окружение воркера."""
    with tempfile.TemporaryDirectory() as tmp:
        окр = dict(os.environ)
        окр["HOME"] = os.path.join(BASE, "dev", "home")
        окр["VS_SERVER_DISABLE_BACKGROUND"] = "1"
        окр["VS_SERVER_DB"] = os.path.join(tmp, "t.db")
        окр["VS_TENANTS_DIR"] = os.path.join(tmp, "tenants")
        окр["VS_SERVER_KEYS_PATH"] = os.path.join(tmp, "keys.json")
        for имя in РУЧКИ:
            окр.pop(имя, None)
        окр.update(окружение)
        скрипт = _ПРОБА_СЕРВЕРА % {"base": BASE, "knobs": list(РУЧКИ)}
        out = subprocess.run([sys.executable, "-c", скрипт], capture_output=True,
                             text=True, env=окр, timeout=180)
    if out.returncode != 0:
        raise SystemExit("подпроцесс сервера упал:\n" + out.stderr[-4000:])
    return json.loads(out.stdout.strip().splitlines()[-1])


def main() -> int:
    print("Воркер: умолчания прежние, пока окружение молчит")
    было = из_воркера(("OPENAI_API_URL", "GPT_MODEL", "SCENE_GPT_MODEL"), {})
    check("адрес прежний — OpenAI", было.get("OPENAI_API_URL"),
          "https://api.openai.com/v1/chat/completions")
    check("модель прежняя — nano", было.get("GPT_MODEL"), "gpt-4.1-nano")
    check("модель сцен прежняя — mini", было.get("SCENE_GPT_MODEL"), "gpt-4.1-mini")

    print("\nВоркер: окружение переставляет и адрес, и модели")
    стало = из_воркера(("OPENAI_API_URL", "GPT_MODEL", "SCENE_GPT_MODEL"), {
        "VS_GPT_CHAT_URL": "https://api.fast-gen.ai/v1/chat/completions",
        "VS_GPT_MODEL": "gpt-4.1-mini",
        "VS_SCENE_GPT_MODEL": "gpt-4o-mini",
    })
    check("адрес переставлен", стало.get("OPENAI_API_URL"),
          "https://api.fast-gen.ai/v1/chat/completions")
    check("модель переставлена", стало.get("GPT_MODEL"), "gpt-4.1-mini")
    check("модель сцен переставлена", стало.get("SCENE_GPT_MODEL"), "gpt-4o-mini")
    # Главная ловушка: у FastGen нано нет вовсе. Если адрес уехал, а модель осталась
    # прежней — прогон упадёт с 400, и это будет выглядеть как поломка, а не настройка.
    только_адрес = из_воркера(("OPENAI_API_URL", "GPT_MODEL"), {
        "VS_GPT_CHAT_URL": "https://api.fast-gen.ai/v1/chat/completions"})
    check("адрес без модели оставляет нано — так делать нельзя, и это видно",
          только_адрес.get("GPT_MODEL"), "gpt-4.1-nano")

    print("\nСервер ПЕРЕДАЁТ ручки воркеру (без этого правка в воркере мертва)")
    пусто = через_сервер({})
    check("молчит окружение — сервер ничего не навязывает",
          {k: v for k, v in пусто.items() if v}, {})
    передано = через_сервер({
        "VS_GPT_CHAT_URL": "https://api.fast-gen.ai/v1/chat/completions",
        "VS_GPT_MODEL": "gpt-4.1-mini",
    })
    check("адрес дошёл до воркера", передано.get("VS_GPT_CHAT_URL"),
          "https://api.fast-gen.ai/v1/chat/completions")
    check("модель дошла до воркера", передано.get("VS_GPT_MODEL"), "gpt-4.1-mini")
    check("незаданное не придумывается", передано.get("VS_SCENE_GPT_MODEL"), None)
    # Пробелы вместо значения — то же, что пусто: иначе воркер получит адрес " " и
    # упадёт на разборе URL, а причина будет неочевидна.
    пробелы = через_сервер({"VS_GPT_CHAT_URL": "   "})
    check("пробелы за значение не считаются", пробелы.get("VS_GPT_CHAT_URL"), None)

    print("\nКлюч чужому адресу: сервер даёт свой, а ключ OpenAI не отдаёт никогда")
    на_fastgen = через_сервер({
        "VS_GPT_CHAT_URL": "https://api.fast-gen.ai/v1/chat/completions"})
    check("для FastGen ключ взят из семейства googler",
          на_fastgen.get("VS_GPT_CHAT_KEY"), "fg-ключ-из-файла")
    check("и это НЕ ключ OpenAI",
          на_fastgen.get("VS_GPT_CHAT_KEY") == "sk-ключ-openai", False)
    # Незнакомому хосту ключ не придумывается: чьим ключом к нему ходить — неизвестно.
    на_чужой = через_сервер({"VS_GPT_CHAT_URL": "https://кто-то-ещё.example/v1/chat"})
    check("незнакомому адресу ключ не подставляется",
          на_чужой.get("VS_GPT_CHAT_KEY"), None)
    явный = через_сервер({"VS_GPT_CHAT_URL": "https://кто-то-ещё.example/v1/chat",
                          "VS_GPT_CHAT_KEY": "свой-ключ"})
    check("но заданный явно — передаётся", явный.get("VS_GPT_CHAT_KEY"), "свой-ключ")
    # Адрес не переставлен — ключ чата не нужен и не передаётся.
    без_адреса = через_сервер({"VS_GPT_CHAT_KEY": "свой-ключ"})
    check("без переставленного адреса ключ чата не передаётся",
          без_адреса.get("VS_GPT_CHAT_KEY"), None)

    print("\nВоркер отказывается до запроса, если адрес чужой, а ключа нет")
    отказ = заголовки({"VS_GPT_CHAT_URL": "https://api.fast-gen.ai/v1/chat/completions"})
    check("отказ, а не подстановка ключа OpenAI",
          отказ.get("ошибка", "").startswith("адрес чата переставлен"), True)
    check("и ключ OpenAI в заголовок не попал", "sk-ключ-openai" in str(отказ), False)
    с_ключом = заголовки({"VS_GPT_CHAT_URL": "https://api.fast-gen.ai/v1/chat/completions",
                          "VS_GPT_CHAT_KEY": "fg-ключ"})
    check("со своим ключом заголовок собирается",
          с_ключом.get("Authorization"), "Bearer fg-ключ")
    прежде = заголовки({})
    check("адрес прежний — ходим ключом OpenAI, как было",
          прежде.get("Authorization"), "Bearer sk-ключ-openai")

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
