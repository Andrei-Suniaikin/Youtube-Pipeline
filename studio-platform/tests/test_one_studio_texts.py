#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Одна студия — и на экране, и в сообщениях сервера (владелец 22.09.2026).

«Смотри, нет больше ни Cover, ни Rewrite Studio — у нас полноценная одна студия, все
упоминания нужно убрать». Внутри по-прежнему три хранилища (тексты, обложки,
производство), но человеку это знать незачем: для него есть «текст», «обложка»,
«озвучка», а не «соседний сервис», «кабинет» и чужие названия.

Тест читает исходники и проверяет строки, которые доходят до экрана: разметку и
строковые литералы пульта (`web/index.html`, без комментариев) и строковые литералы
сервера пульта (`web/app.py`, без комментариев и документации).

    .venv/bin/python tests/test_one_studio_texts.py
"""
from __future__ import annotations

import ast
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
WORDS = re.compile(r"Cover Studio|Rewrite Studio|соседн|сосед[аяеуи]?\b|кабинет", re.I)
# Отдельные «Rewrite» и «Cover» с большой буквы — имена бывших студий («в разделе
# Rewrite»). Строчные «rewrite»/«cover» — внутренние коды сервисов, их не трогаем.
NAMES = re.compile(r"\bRewrite\b|\bCover\b")
# Модули сервера, чьи строки доходят до экрана: подсказки полей пресета, тревоги,
# причины забора текстов и журнал выпуска.
SERVER_MODULES = ("web/app.py", "server/preset_fields.py", "server/pub_alerts.py",
                  "server/pub_services.py", "server/self_setup.py")
# 23.09.2026: файлы выкатки тоже говорят с человеком — владелец споткнулся ровно о них
# («что это за приколы такие, есть одна студия»). Это не Python-модули, поэтому строки
# в них ищем построчно, пропуская комментарии.
DEPLOY_FILES = ("deploy/make_admin.py", "deploy/start-studio.sh", "deploy/update.sh",
                "deploy/ПЕРЕДАЧА.md")
FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}\n        ожидалось: {want!r}\n        получено:  {got!r}")
        FAILS.append(label)


def page_hits() -> list:
    html = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
    # Комментарии — не экран: /* … */ и // до конца строки.
    code = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), html, flags=re.S)
    # Имена в коде — латиницей, поэтому после снятия комментариев любое из этих слов —
    # это текст экрана: разметка, строка или шаблон (в том числе многострочный).
    hits = []
    for n, line in enumerate(code.split("\n"), 1):
        line = re.sub(r"(^|[^:'\"`])//.*$", r"\1", line)
        m = WORDS.search(line) or NAMES.search(line)
        if m:
            hits.append(f"{n}: {line.strip()[:80]}")
    return hits


def deploy_hits() -> list:
    """Что увидит человек, читающий вывод скриптов выкатки и памятку."""
    hits = []
    for name in DEPLOY_FILES:
        path = os.path.join(BASE, name)
        if not os.path.exists(path):
            continue
        for n, line in enumerate(open(path, encoding="utf-8"), 1):
            bare = line.strip()
            # Комментарий — разговор разработчиков между собой, человеку он не виден.
            if bare.startswith("#") or bare.startswith("//"):
                continue
            if WORDS.search(line) or NAMES.search(line):
                hits.append(f"{name}:{n}: {bare[:80]}")
    return hits


def server_hits() -> list:
    hits = []
    for mod in SERVER_MODULES:
        hits += [f"{mod}:{h}" for h in module_hits(mod)]
    return hits


def module_hits(mod: str) -> list:
    src = open(os.path.join(BASE, mod), encoding="utf-8").read()
    tree = ast.parse(src)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            if node.body and isinstance(node.body[0], ast.Expr) and \
                    isinstance(getattr(node.body[0], "value", None), ast.Constant):
                docs.add(id(node.body[0].value))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            if WORDS.search(node.value) or NAMES.search(node.value):
                hits.append(f"{node.lineno}: {node.value[:80]}")
    return hits


def main() -> int:
    print("Проверка: одна студия — без «Cover/Rewrite Studio», «соседа» и «кабинета»")
    check("на экране пульта таких слов нет", page_hits(), [])
    check("в сообщениях сервера тоже", server_hits(), [])
    check("и в том, что печатает выкатка", deploy_hits(), [])
    print()
    if FAILS:
        print(f"ПРОВАЛЕНО: {len(FAILS)}")
        return 1
    print("все проверки прошли")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
